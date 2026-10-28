"use strict";

// Apply the stored or system theme before the stylesheet renders to avoid a flash.
(() => {
  const key = "wm-theme";
  const system = window.matchMedia("(prefers-color-scheme: dark)");
  let saved = null;

  try {
    const value = window.localStorage.getItem(key);
    if (value === "light" || value === "dark") saved = value;
  } catch (_) {
    // Browser storage may be unavailable; the toggle still works for this page.
  }

  function apply(theme) {
    const dark = theme === "dark";
    document.documentElement.dataset.theme = theme;
    document.documentElement.style.colorScheme = theme;

    const button = document.getElementById("theme-toggle");
    if (!button) return;
    const label = dark ? "Switch to light mode" : "Switch to dark mode";
    button.setAttribute("aria-label", label);
    button.setAttribute("title", label);
    button.setAttribute("aria-pressed", String(dark));
    button.querySelector(".theme-label").textContent = dark ? "Light mode" : "Dark mode";
  }

  function sync() {
    apply(saved || (system.matches ? "dark" : "light"));
  }

  sync();

  document.addEventListener("DOMContentLoaded", () => {
    sync();
    const button = document.getElementById("theme-toggle");
    if (!button) return;
    button.addEventListener("click", () => {
      saved = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
      try {
        window.localStorage.setItem(key, saved);
      } catch (_) {
        // Continue with the in-memory preference.
      }
      sync();
    });
  });

  if (typeof system.addEventListener === "function") {
    system.addEventListener("change", () => {
      if (!saved) sync();
    });
  }

  window.addEventListener("storage", (event) => {
    if (event.key !== key && event.key !== null) return;
    saved = event.newValue === "dark" || event.newValue === "light" ? event.newValue : null;
    sync();
  });
})();
