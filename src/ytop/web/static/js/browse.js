import { $, $$, storageGet, storageSet } from "./core.js";

// -- library home: Browse all ----------------------------------------------
// Chips filter every section (AND across rows, OR within a row). Grouping
// clones a card into each group it belongs to; sorting applies per group.

const KEYS = {
  view: "ytop.browse.view",
  group: "ytop.browse.group",
  sort: "ytop.browse.sort",
};
const VIEWS = ["grid", "list"];
const FACETS = ["category", "tags", "channel"];

function isTextField(node) {
  if (!node || node === document.body) return false;
  if (node.isContentEditable) return true;
  const tag = node.tagName;
  if (tag === "TEXTAREA" || tag === "SELECT") return true;
  if (tag !== "INPUT") return false;
  const type = (node.getAttribute("type") || "text").toLowerCase();
  return !["button", "checkbox", "radio", "range", "submit", "reset", "color", "file"].includes(type);
}

function initSlashSearch() {
  document.addEventListener("keydown", (event) => {
    if (event.key !== "/" || event.ctrlKey || event.metaKey || event.altKey) return;
    if (event.defaultPrevented || isTextField(event.target)) return;
    const input = $(".topbar .search input[type=search]");
    if (!input) return;
    event.preventDefault();
    input.focus();
    input.select();
  });
}

function restoreSelect(select, key) {
  if (!select) return;
  const stored = storageGet(key, null);
  if (stored !== null && Array.from(select.options).some((o) => o.value === stored)) {
    select.value = stored;
  }
}

function num(card, field) {
  const value = Number(card.dataset[field]);
  return Number.isFinite(value) ? value : 0;
}

function byTitle(a, b) {
  return (a.dataset.title || "").localeCompare(b.dataset.title || "", undefined, {
    sensitivity: "base",
    numeric: true,
  });
}

function comparator(mode) {
  const primary = {
    recent: (a, b) => (b.dataset.updated || "").localeCompare(a.dataset.updated || ""),
    title: () => 0,
    length: (a, b) => num(b, "length") - num(a, "length"),
    remaining: (a, b) => num(b, "remaining") - num(a, "remaining"),
    size: (a, b) => num(b, "bytes") - num(a, "bytes"),
  }[mode] || (() => 0);
  return (a, b) => primary(a, b) || byTitle(a, b);
}

function cardValues(card, facet) {
  const raw = card.dataset[facet] || "";
  if (facet === "tags") return raw.split("|").filter(Boolean);
  return raw ? [raw] : [];
}

export function initBrowse() {
  initSlashSearch();
  const results = $("#browse-results");
  if (!results) return;
  const master = $("[data-browse-all]", results);
  const cards = master ? $$(".playlist-card", master) : [];
  const total = cards.length;
  const others = $$("[data-browse-section]").filter((node) => !results.contains(node));
  const groupSelect = $("#browse-group");
  const sortSelect = $("#browse-sort");
  const viewButtons = $$(".view-toggle [data-view]");
  const count = $("#browse-count");
  const empty = $("#browse-empty");
  const chipRows = $$(".chip-filter-row[data-facet]");

  const tagLabels = new Map();
  $$('.chip-filter-row[data-facet="tags"] .chip').forEach((chip) => {
    tagLabels.set(chip.dataset.value, chip.textContent.trim());
  });

  const selected = Object.fromEntries(FACETS.map((facet) => [facet, new Set()]));

  function matches(card) {
    return FACETS.every((facet) => {
      const wanted = selected[facet];
      if (!wanted.size) return true;
      return cardValues(card, facet).some((value) => wanted.has(value));
    });
  }

  function groupsFor(card, mode) {
    if (mode === "tag") {
      const tags = cardValues(card, "tags");
      return tags.length ? tags : [""];
    }
    if (mode === "library") return [card.dataset.library || ""];
    return [card.dataset[mode] || ""];
  }

  function groupLabel(key, mode, card) {
    if (mode === "tag") return key ? tagLabels.get(key) || key : "Untagged";
    if (mode === "category") return key || "Uncategorized";
    if (mode === "channel") return key || "Unknown channel";
    if (mode === "library") return card.dataset.libraryLabel || key;
    return key;
  }

  function grid(items) {
    const node = document.createElement("div");
    node.className = "playlist-grid";
    items.forEach((card) => node.appendChild(card));
    return node;
  }

  function render() {
    const mode = groupSelect ? groupSelect.value : "none";
    const sorter = comparator(sortSelect ? sortSelect.value : "recent");
    const shown = cards.filter(matches);
    results.replaceChildren();
    if (mode === "none") {
      results.appendChild(grid(shown.slice().sort(sorter)));
    } else {
      const groups = new Map();
      shown.forEach((card) => {
        groupsFor(card, mode).forEach((key) => {
          if (!groups.has(key)) groups.set(key, { label: groupLabel(key, mode, card), items: [] });
          groups.get(key).items.push(card);
        });
      });
      const ordered = Array.from(groups.entries()).sort(([ka, a], [kb, b]) => {
        if (!ka !== !kb) return ka ? -1 : 1; // the "none" group goes last
        return a.label.localeCompare(b.label, undefined, { sensitivity: "base" });
      });
      ordered.forEach(([, group]) => {
        const section = document.createElement("section");
        section.className = "browse-group";
        const heading = document.createElement("h3");
        heading.className = "browse-group-title";
        heading.textContent = group.label;
        const tally = document.createElement("span");
        tally.className = "muted small";
        tally.textContent = ` ${group.items.length}`;
        heading.appendChild(tally);
        section.appendChild(heading);
        section.appendChild(
          grid(group.items.slice().sort(sorter).map((card) => card.cloneNode(true)))
        );
        results.appendChild(section);
      });
    }
    others.forEach((rail) => {
      let visible = 0;
      $$(".playlist-card", rail).forEach((card) => {
        const hit = matches(card);
        card.hidden = !hit;
        if (hit) visible += 1;
      });
      const section = rail.closest(".home-section");
      if (section) section.hidden = visible === 0;
    });
    if (count) {
      const noun = `playlist${total === 1 ? "" : "s"}`;
      count.textContent = shown.length === total
        ? `${total} ${noun}`
        : `${shown.length} of ${total} ${noun}`;
    }
    if (empty) empty.hidden = shown.length > 0;
  }

  function setView(view) {
    const value = VIEWS.includes(view) ? view : "grid";
    results.dataset.view = value;
    viewButtons.forEach((button) => {
      button.setAttribute("aria-pressed", button.dataset.view === value ? "true" : "false");
    });
    return value;
  }

  setView(storageGet(KEYS.view, "grid"));
  viewButtons.forEach((button) => {
    button.addEventListener("click", () => {
      storageSet(KEYS.view, setView(button.dataset.view));
    });
  });

  restoreSelect(groupSelect, KEYS.group);
  restoreSelect(sortSelect, KEYS.sort);
  if (groupSelect) {
    groupSelect.addEventListener("change", () => {
      storageSet(KEYS.group, groupSelect.value);
      render();
    });
  }
  if (sortSelect) {
    sortSelect.addEventListener("change", () => {
      storageSet(KEYS.sort, sortSelect.value);
      render();
    });
  }

  // Long chip rows show the first few chips; "More" reveals the rest. A
  // pressed chip is never hidden.
  function syncRow(row) {
    const more = $("[data-more]", row);
    if (!more) return;
    const open = more.getAttribute("aria-expanded") === "true";
    $$("button.chip[data-extra]", row).forEach((chip) => {
      chip.hidden = !open && chip.getAttribute("aria-pressed") !== "true";
    });
    more.textContent = open ? "Fewer" : "More";
  }

  chipRows.forEach((row) => {
    const facet = row.dataset.facet;
    const more = $("[data-more]", row);
    if (more) {
      more.addEventListener("click", () => {
        const open = more.getAttribute("aria-expanded") !== "true";
        more.setAttribute("aria-expanded", open ? "true" : "false");
        syncRow(row);
      });
    }
    row.addEventListener("click", (event) => {
      const chip = event.target.closest("button.chip");
      if (!chip || !row.contains(chip) || !selected[facet]) return;
      const value = chip.dataset.value || "";
      const on = chip.getAttribute("aria-pressed") !== "true";
      chip.setAttribute("aria-pressed", on ? "true" : "false");
      if (on) selected[facet].add(value);
      else selected[facet].delete(value);
      syncRow(row);
      render();
    });
  });

  render();
  // Live only from now on: the count written on load is not announced.
  if (count) count.setAttribute("role", "status");
}
