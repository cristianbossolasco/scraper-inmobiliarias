(() => {
  const enhancedForms = ["search-form", "stats-filter-form"];
  const ignoredNames = new Set(["south", "west", "north", "east", "radius_lat", "radius_lng", "radius_km", "polygon"]);

  function cssEscape(value) {
    if (window.CSS?.escape) return CSS.escape(value);
    return String(value || "").replace(/[^a-zA-Z0-9_-]/g, "\\$&");
  }

  function labelFor(control) {
    const form = control.closest("form");
    const label = control.id ? form?.querySelector(`label[for="${cssEscape(control.id)}"]`) : null;
    return (label?.childNodes?.[0]?.textContent || control.name || "Filtro").trim();
  }

  function optionText(option) {
    return (option?.textContent || option?.value || "").replace(/\s+/g, " ").trim();
  }

  function selectedOptions(select) {
    return Array.from(select.options).filter((option) => option.selected && option.value !== "");
  }

  function closePopovers(except = null) {
    document.querySelectorAll(".smart-select-popover:not([hidden])").forEach((popover) => {
      if (popover === except) return;
      popover.hidden = true;
      popover.closest(".smart-select")?.querySelector(".smart-select-button")?.setAttribute("aria-expanded", "false");
    });
  }

  function updateSmartSelect(select) {
    const shell = select.nextElementSibling?.classList?.contains("smart-select") ? select.nextElementSibling : null;
    if (!shell) return;
    const selected = selectedOptions(select);
    shell.querySelector(".smart-select-button strong").textContent = selected.length
      ? `${selected.length} seleccionada${selected.length === 1 ? "" : "s"}`
      : optionText(select.options[0]) || "Todas";
    const chips = shell.querySelector(".smart-select-chips");
    chips.innerHTML = "";
    selected.slice(0, 4).forEach((option) => {
      const chip = document.createElement("span");
      chip.textContent = optionText(option);
      chips.appendChild(chip);
    });
    if (selected.length > 4) {
      const more = document.createElement("span");
      more.textContent = `+${selected.length - 4}`;
      chips.appendChild(more);
    }
    shell.querySelectorAll("[data-smart-option]").forEach((checkbox) => {
      checkbox.checked = Boolean(Array.from(select.options).find((option) => option.value === checkbox.value)?.selected);
    });
  }

  function buildOption(select, option) {
    if (option.value === "") {
      const clear = document.createElement("button");
      clear.type = "button";
      clear.className = "smart-select-clear";
      clear.textContent = optionText(option);
      clear.addEventListener("click", () => {
        Array.from(select.options).forEach((item) => { item.selected = false; });
        updateSmartSelect(select);
        select.dispatchEvent(new Event("change", { bubbles: true }));
      });
      return clear;
    }
    const row = document.createElement("label");
    row.className = "smart-select-option";
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.value = option.value;
    checkbox.checked = option.selected;
    checkbox.dataset.smartOption = "1";
    const text = document.createElement("span");
    text.textContent = optionText(option);
    row.append(checkbox, text);
    checkbox.addEventListener("change", () => {
      option.selected = checkbox.checked;
      updateSmartSelect(select);
      select.dispatchEvent(new Event("change", { bubbles: true }));
    });
    return row;
  }

  function enhanceSelect(select) {
    if (select.dataset.smartSelectReady === "1") return;
    select.dataset.smartSelectReady = "1";
    select.classList.add("native-smart-select");
    const shell = document.createElement("div");
    shell.className = "smart-select";
    shell.innerHTML = `
      <button class="smart-select-button" type="button" aria-expanded="false">
        <span>${labelFor(select)}</span>
        <strong></strong>
        <i data-lucide="chevron-down"></i>
      </button>
      <div class="smart-select-popover" hidden>
        <input class="smart-select-search" type="search" placeholder="Filtrar opciones">
        <div class="smart-select-options"></div>
      </div>
      <div class="smart-select-chips"></div>
    `;
    const optionsWrap = shell.querySelector(".smart-select-options");
    Array.from(select.options).forEach((option) => optionsWrap.appendChild(buildOption(select, option)));
    select.insertAdjacentElement("afterend", shell);
    const button = shell.querySelector(".smart-select-button");
    const popover = shell.querySelector(".smart-select-popover");
    const search = shell.querySelector(".smart-select-search");
    button.addEventListener("click", () => {
      const willOpen = popover.hidden;
      closePopovers(popover);
      popover.hidden = !willOpen;
      button.setAttribute("aria-expanded", String(willOpen));
      if (willOpen) search.focus();
    });
    search.addEventListener("input", () => {
      const term = search.value.trim().toLowerCase();
      shell.querySelectorAll(".smart-select-option").forEach((row) => {
        row.hidden = Boolean(term) && !row.textContent.toLowerCase().includes(term);
      });
    });
    updateSmartSelect(select);
  }

  function updateSummary(form) {
    const summary = form.querySelector("[data-filter-summary]");
    if (!summary) return;
    const chips = [];
    form.querySelectorAll("input,select").forEach((control) => {
      if (!control.name || ignoredNames.has(control.name)) return;
      if (["hidden", "submit", "button"].includes(control.type)) return;
      if (control.matches(".zone-search,.smart-select-search")) return;
      if (control.tagName === "SELECT" && control.multiple) {
        const selected = selectedOptions(control);
        if (selected.length) {
          chips.push(`${labelFor(control)}: ${selected.map(optionText).slice(0, 2).join(", ")}${selected.length > 2 ? ` +${selected.length - 2}` : ""}`);
        }
      } else if (control.type === "checkbox") {
        if (control.checked) chips.push(`${control.name}: ${control.value}`);
      } else if (control.value) {
        chips.push(`${labelFor(control)}: ${control.value}`);
      }
    });
    summary.innerHTML = "";
    (chips.length ? chips.slice(0, 8) : ["Sin filtros adicionales"]).forEach((chip) => {
      const node = document.createElement("span");
      node.textContent = chip;
      summary.appendChild(node);
    });
    if (chips.length > 8) {
      const more = document.createElement("span");
      more.textContent = `+${chips.length - 8} filtros`;
      summary.appendChild(more);
    }
  }

  function markDirty(form) {
    if (!form || form.dataset.initializedDirty === "0") return;
    form.classList.add("filters-dirty");
    const status = form.querySelector("[data-filter-dirty-status]") || document.querySelector("[data-filter-dirty-status]");
    if (status) status.textContent = "Cambios sin aplicar";
    updateSummary(form);
  }

  function enhanceForm(form) {
    if (!form || form.dataset.filterPanelReady === "1") return;
    form.dataset.filterPanelReady = "1";
    form.dataset.initializedDirty = "0";
    form.querySelectorAll("select[multiple]").forEach(enhanceSelect);
    updateSummary(form);
    form.dataset.initializedDirty = "1";
    form.addEventListener("input", () => markDirty(form));
    form.addEventListener("change", () => markDirty(form));
    form.addEventListener("submit", () => {
      form.classList.remove("filters-dirty");
      form.classList.add("filters-submitting");
      const status = form.querySelector("[data-filter-dirty-status]") || document.querySelector("[data-filter-dirty-status]");
      if (status) status.textContent = "Aplicando filtros...";
    });
  }

  function init() {
    enhancedForms.map((id) => document.getElementById(id)).forEach(enhanceForm);
    if (window.lucide) lucide.createIcons();
  }

  document.addEventListener("click", (event) => {
    if (!event.target.closest(".smart-select")) closePopovers();
  });
  document.addEventListener("DOMContentLoaded", init);
  document.body.addEventListener("htmx:afterSwap", init);
})();
