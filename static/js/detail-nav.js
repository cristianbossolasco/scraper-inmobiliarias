(() => {
  document.addEventListener("keydown", (event) => {
    if (["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName)) return;
    if (event.key === "ArrowLeft" && window.RADAR_PREVIOUS_URL) {
      window.location.href = window.RADAR_PREVIOUS_URL;
    }
    if (event.key === "ArrowRight" && window.RADAR_NEXT_URL) {
      window.location.href = window.RADAR_NEXT_URL;
    }
  });
})();
