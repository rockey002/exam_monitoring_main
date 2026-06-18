function byId(id) {
  return document.getElementById(id);
}

function normalize(text) {
  return (text || "").toLowerCase().trim();
}

function applyEventFilters() {
  const query = normalize(byId("event-search")?.value);
  const source = normalize(byId("event-source")?.value);
  const severity = normalize(byId("event-severity")?.value);
  const rows = document.querySelectorAll("#events-body tr[data-row='event']");
  let visible = 0;

  rows.forEach((row) => {
    const rowText = normalize(row.getAttribute("data-text"));
    const rowSource = normalize(row.getAttribute("data-source"));
    const rowSeverity = normalize(row.getAttribute("data-severity"));
    const queryMatch = !query || rowText.includes(query);
    const sourceMatch = !source || rowSource === source;
    const severityMatch = !severity || rowSeverity === severity;
    const show = queryMatch && sourceMatch && severityMatch;
    row.style.display = show ? "" : "none";
    if (show) visible += 1;
  });

  const counter = byId("event-count");
  if (counter) counter.textContent = String(visible);
}

function initAdminFilters() {
  ["event-search", "event-source", "event-severity"].forEach((id) => {
    const el = byId(id);
    if (el) el.addEventListener("input", applyEventFilters);
    if (el) el.addEventListener("change", applyEventFilters);
  });

  const resetBtn = byId("event-reset");
  if (resetBtn) {
    resetBtn.addEventListener("click", () => {
      byId("event-search").value = "";
      byId("event-source").value = "";
      byId("event-severity").value = "";
      applyEventFilters();
    });
  }

  applyEventFilters();
}

document.addEventListener("DOMContentLoaded", initAdminFilters);
