import { $, $$ } from "./core.js";

// -- navigation -----------------------------------------------------------
export function initNav() {
  const toggle = $("#nav-toggle");
  const nav = $("#site-nav");
  if (!toggle || !nav) return;
  toggle.addEventListener("click", () => {
    const open = nav.classList.toggle("open");
    toggle.setAttribute("aria-expanded", open ? "true" : "false");
  });
}

export function initAddMenu() {
  const menu = $(".add-menu");
  if (!menu) return;
  const summary = menu.querySelector("summary");
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || !menu.open) return;
    menu.open = false;
    if (summary) summary.focus();
  });
  document.addEventListener("click", (event) => {
    if (menu.open && !menu.contains(event.target)) menu.open = false;
  });
  // Tabbing out of the menu closes it.  relatedTarget is where focus went;
  // it is null for a click on plain text, which the click handler covers.
  menu.addEventListener("focusout", (event) => {
    const next = event.relatedTarget;
    if (menu.open && next && !menu.contains(next)) menu.open = false;
  });
}

// -- dialogs and submit safety -------------------------------------------
export function initDialogs() {
  const dialog = $("#confirm-dialog");
  const message = $("#confirm-message");
  if (dialog && message) {
    document.addEventListener("submit", (event) => {
      const form = event.target;
      if (!form.matches || !form.matches("form[data-confirm]")) return;
      if (form.dataset.confirmed === "1") {
        delete form.dataset.confirmed;
        return;
      }
      event.preventDefault();
      const prompt = form.dataset.confirm || "Are you sure?";
      if (typeof dialog.showModal !== "function") {
        if (window.confirm(prompt)) {
          form.dataset.confirmed = "1";
          form.requestSubmit();
        }
        return;
      }
      const accept = $("#confirm-accept");
      if (accept) {
        accept.textContent = form.dataset.confirmAction || "Confirm";
      }
      message.textContent = prompt;
      dialog.returnValue = "";
      const decide = () => {
        dialog.removeEventListener("close", decide);
        if (dialog.returnValue === "confirm") {
          form.dataset.confirmed = "1";
          form.requestSubmit();
        }
      };
      dialog.addEventListener("close", decide);
      dialog.showModal();
    });
  }
  // Prevent accidental double submits; server-side validation still wins.
  document.addEventListener("submit", (event) => {
    if (event.defaultPrevented) return;
    const form = event.target;
    if (!form || !form.querySelectorAll) return;
    window.setTimeout(() => {
      form.querySelectorAll('button[type="submit"]').forEach((button) => {
        button.disabled = true;
        button.setAttribute("aria-busy", "true");
      });
    }, 0);
  });
}

export function syncAutoplayLinks() {
  const toggle = $("#autoplay-toggle");
  if (!toggle) return;
  $$('a[href*="/watch/"]').forEach((link) => {
    const url = new URL(link.href, window.location.origin);
    url.searchParams.set("autoplay", toggle.checked ? "1" : "0");
    link.href = url.pathname + url.search + url.hash;
  });
}

// -- tabs -----------------------------------------------------------------
export function initTabs() {
  const tabs = $$(".tab");
  const panels = $$(".tab-panel");
  if (!tabs.length) return;

  function select(tab, moveFocus = false) {
    tabs.forEach((other) => {
      const active = other === tab;
      other.classList.toggle("active", active);
      other.setAttribute("aria-selected", active ? "true" : "false");
      other.tabIndex = active ? 0 : -1;
    });
    panels.forEach((panel) => {
      panel.hidden = panel.dataset.panel !== tab.dataset.tab;
    });
    if (moveFocus) tab.focus();
    if (tab.dataset.tab === "transcript") {
      const active = $(".transcript li.active");
      if (active) active.scrollIntoView({ block: "nearest" });
    }
  }

  tabs.forEach((tab, index) => {
    tab.addEventListener("click", () => select(tab));
    tab.addEventListener("keydown", (event) => {
      let next = null;
      if (event.key === "ArrowRight") next = (index + 1) % tabs.length;
      else if (event.key === "ArrowLeft") {
        next = (index - 1 + tabs.length) % tabs.length;
      } else if (event.key === "Home") next = 0;
      else if (event.key === "End") next = tabs.length - 1;
      if (next === null) return;
      event.preventDefault();
      select(tabs[next], true);
    });
  });
  const initial = tabs.find((tab) => tab.classList.contains("active")) || tabs[0];
  select(initial);
}
