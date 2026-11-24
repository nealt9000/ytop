import { $, $$, storageGet, storageSet } from "./core.js";

// -- library home: playlist grid -------------------------------------------
// The chip strip (one category at a time, plus "Not started") and the filter
// box (every word must appear in a card's title, channel, category, tags or
// library) narrow the grid only; Continue watching is never filtered.
// Grouping clones a card into each group it belongs to; sorting applies per
// group.

const KEYS = {
  view: "ytop.browse.view",
  group: "ytop.browse.group",
  sort: "ytop.browse.sort",
};
const VIEWS = ["grid", "list"];

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

function splitList(raw) {
  return (raw || "").split("|").filter(Boolean);
}

// Tag labels arrive as a JSON list: a label may itself contain "|".
function tagLabelsOf(card) {
  try {
    const labels = JSON.parse(card.dataset.tagLabels || "[]");
    return Array.isArray(labels) ? labels.map(String) : [];
  } catch {
    return [];
  }
}

function haystack(card) {
  const d = card.dataset;
  return [d.title, d.channel, d.category, tagLabelsOf(card).join(" "), d.libraryLabel]
    .filter(Boolean)
    .join(" ")
    .toLocaleLowerCase();
}

export function initBrowse() {
  initSlashSearch();
  const results = $("#browse-results");
  if (!results) return;
  const master = $("[data-browse-all]", results);
  const cards = master ? $$(".playlist-card", master) : [];
  const total = cards.length;
  const groupSelect = $("#browse-group");
  const sortSelect = $("#browse-sort");
  const viewButtons = $$(".view-toggle [data-view]");
  const count = $("#browse-count");
  const empty = $("#browse-empty");
  const strip = $("#browse-chips");
  const filterBox = $("#browse-filter");

  // Tag keys to labels, for Group by -> Tag (keys and labels share an order).
  const tagLabels = new Map();
  const hay = new Map();
  cards.forEach((card) => {
    const keys = splitList(card.dataset.tags);
    const labels = tagLabelsOf(card);
    keys.forEach((key, i) => {
      if (!tagLabels.has(key)) tagLabels.set(key, labels[i] || key);
    });
    hay.set(card, haystack(card));
  });

  const state = { category: null, notStarted: false, words: [] };

  function matches(card) {
    if (state.category !== null && card.dataset.category !== state.category) return false;
    if (state.notStarted && card.dataset.started !== "0") return false;
    const text = hay.get(card) || haystack(card);
    return state.words.every((word) => text.includes(word));
  }

  function groupsFor(card, mode) {
    if (mode === "tag") {
      const tags = splitList(card.dataset.tags);
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

  function syncChips() {
    if (!strip) return;
    $$("button.chip[data-chip]", strip).forEach((chip) => {
      const kind = chip.dataset.chip;
      let on = false;
      if (kind === "all") on = state.category === null && !state.notStarted;
      else if (kind === "not-started") on = state.notStarted;
      else on = state.category === (chip.dataset.value || "");
      chip.setAttribute("aria-pressed", on ? "true" : "false");
    });
  }

  if (strip) {
    strip.addEventListener("click", (event) => {
      const chip = event.target.closest("button.chip[data-chip]");
      if (!chip || !strip.contains(chip)) return;
      const kind = chip.dataset.chip;
      if (kind === "all") {
        state.category = null;
        state.notStarted = false;
      } else if (kind === "not-started") {
        state.notStarted = !state.notStarted;
      } else {
        const value = chip.dataset.value || "";
        state.category = state.category === value ? null : value;
      }
      syncChips();
      render();
    });
  }

  if (filterBox) {
    filterBox.addEventListener("input", () => {
      state.words = filterBox.value.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
      render();
    });
  }

  render();
  // Live only from now on: the count written on load is not announced.
  if (count) count.setAttribute("role", "status");
}
