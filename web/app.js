"use strict";

const fold = value => value.normalize("NFKC").toLocaleLowerCase();
const form = document.querySelector("#filters");
if (form) {
  form.hidden = false;
  const entries = [...document.querySelectorAll("[data-entry]")];
  const applyFilters = () => {
    const query = fold(form.elements.q.value.trim());
    const kind = form.elements.kind.value;
    let count = 0;
    for (const entry of entries) {
      entry.hidden = !(fold(entry.dataset.search).includes(query)
        && (!kind || entry.dataset.kind === kind)
        && (!form.elements.available.checked || entry.dataset.available === "true"));
      if (!entry.hidden) count++;
    }
    for (const section of document.querySelectorAll(".product")) {
      section.hidden = ![...section.querySelectorAll("[data-entry]")].some(entry => !entry.hidden);
    }
    document.querySelector("#filter-status").textContent = `${count} 件の配布物`;
  };
  form.addEventListener("submit", event => event.preventDefault());
  form.addEventListener("input", applyFilters);
  applyFilters();
}

// A same-origin streaming response can set Content-Disposition, unlike the
// download attribute on a cross-origin HF link. No file-sized Blob is needed.
const script = new URL(document.currentScript.src);
if ("serviceWorker" in navigator && window.isSecureContext) {
  navigator.serviceWorker.register(new URL("sw.js", script), {scope: new URL(".", script).pathname})
    .then(() => navigator.serviceWorker.ready)
    .then(registration => {
      const enable = () => {
        // Downloads are navigations into the registered scope; the current
        // document need not already have a controller (Firefox install race).
        if (!registration.active) return;
        for (const link of document.querySelectorAll("a.download")) {
          link.href = new URL(`download/${link.dataset.id}`, script);
        }
      };
      navigator.serviceWorker.addEventListener("controllerchange", enable);
      enable();
    })
    // The ordinary download link remains usable if registration is unavailable.
    .catch(() => {});
}
