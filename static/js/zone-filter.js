(() => {
  function normalize(value) {
    return (String(value || ""))
      .normalize("NFD")
      .replace(/[\\u0300-\\u036f]/g, "")
      .toLowerCase()
      .replace(/[^a-z0-9\s]/g, "")
      .replace(/\s+/g, " ")
      .trim();
  }

  function initZoneFilter(root = document) {
    root.querySelectorAll(".zone-filter").forEach((filter) => {
      if (filter.dataset.zoneFilterReady === "1") {
        return;
      }
      filter.dataset.zoneFilterReady = "1";
      const search = filter.querySelector(".zone-search");
      const options = Array.from(filter.querySelectorAll(".zone-option"));
      const selectedWrap = filter.querySelector(".zone-selected");
      const summary = filter.querySelector(".zone-summary span");
      const clear = filter.querySelector(".zone-clear");

      function selectedOptions() {
        return options.filter((option) => option.querySelector("input").checked);
      }

      function renderSelected() {
        const selected = selectedOptions();
        if (summary) {
          summary.textContent = `${selected.length} seleccionada${selected.length === 1 ? "" : "s"}`;
        }
        if (!selectedWrap) {
          return;
        }
        selectedWrap.innerHTML = "";
        selected.forEach((option) => {
          const input = option.querySelector("input");
          const button = document.createElement("button");
          button.type = "button";
          button.className = "zone-chip";
          button.textContent = input.value;
          button.title = `Quitar ${input.value}`;
          button.addEventListener("click", () => {
            input.checked = false;
            option.dataset.selected = "0";
            applyFilter();
          });
          selectedWrap.appendChild(button);
        });
      }

      function applyFilter() {
        const term = normalize(search?.value || "");
        const hasTerm = term.length > 0;
        options.forEach((option) => {
          const input = option.querySelector("input");
          const selected = input.checked;
          option.dataset.selected = selected ? "1" : "0";
          const name = normalize(option.dataset.zoneName || option.querySelector("span")?.textContent || "");
          const visible = selected || (hasTerm && name.includes(term));
          option.hidden = !visible;
        });
        renderSelected();
      }

      search?.addEventListener("input", applyFilter);
      search?.addEventListener("keydown", (event) => {
        if (event.key === "Enter") {
          event.preventDefault();
        }
      });
      options.forEach((option) => {
        option.querySelector("input").addEventListener("change", () => {
          applyFilter();
        });
      });
      clear?.addEventListener("click", () => {
        options.forEach((option) => {
          option.querySelector("input").checked = false;
          option.dataset.selected = "0";
        });
        if (search) {
          search.value = "";
        }
        applyFilter();
      });
      applyFilter();
    });
  }

  document.addEventListener("DOMContentLoaded", () => initZoneFilter());
  document.body.addEventListener("htmx:afterSwap", (event) => initZoneFilter(event.target));
})();
