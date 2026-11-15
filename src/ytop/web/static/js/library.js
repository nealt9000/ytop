import { $, $$ } from "./core.js";

// -- playlist queue -------------------------------------------------------
export function initQueue() {
  const current = $(".queue-item.current");
  if (current) current.scrollIntoView({ block: "nearest" });
}

// -- playlist filtering --------------------------------------------------
export function initPlaylistFilter() {
  const input = $("#playlist-filter");
  const state = $("#playlist-filter-state");
  const count = $("#playlist-count");
  const list = $(".video-list");
  if (!list || (!input && !state)) return;
  const rows = $$(".video-row", list);
  const total = rows.length;

  function apply() {
    const needle = input ? input.value.trim().toLowerCase() : "";
    const mode = state ? state.value : "all";
    let shown = 0;
    rows.forEach((row) => {
      const matchesText = !needle || (row.dataset.search || "").includes(needle);
      const watched = row.dataset.watched === "1";
      const available = row.dataset.available !== "0";
      const matchesState =
        mode === "all"
        || (mode === "watched" && watched)
        || (mode === "unwatched" && !watched)
        || (mode === "missing" && !available);
      const hit = matchesText && matchesState;
      row.hidden = !hit;
      if (hit) shown += 1;
    });
    if (count) {
      count.textContent = shown === total
        ? `${total} video${total === 1 ? "" : "s"}`
        : `${shown} of ${total} shown`;
    }
  }

  if (input) input.addEventListener("input", apply);
  if (state) state.addEventListener("change", apply);
  list.addEventListener("ytop:editorder", (event) => {
    if (event.detail && event.detail.editing) {
      if (input) input.value = "";
      if (state) state.value = "all";
    }
    apply();
  });
  apply();
}

export function initReorder() {
  const editButton = $("#order-edit");
  const saveButton = $("#order-save");
  const hint = $("#order-hint");
  const form = $("#order-form");
  const list = $(".video-list");
  if (!editButton || !saveButton || !form || !list) return;

  const rows = () => $$(".video-row", list);
  const original = rows();
  let editing = false;
  let dragged = null;

  function applyMode() {
    list.classList.toggle("reorder-mode", editing);
    document.body.classList.toggle("reorder-mode", editing);
    editButton.textContent = editing ? "Cancel" : "✎ Edit order";
    editButton.classList.toggle("primary", editing);
    editButton.classList.toggle("ghost", !editing);
    saveButton.hidden = !editing;
    if (hint) hint.hidden = !editing;
    rows().forEach((row) => {
      row.draggable = editing;
      row.tabIndex = editing ? 0 : -1;
    });
    list.dispatchEvent(new CustomEvent("ytop:editorder", {
      detail: { editing },
    }));
  }

  function restore() {
    original.forEach((row) => list.appendChild(row));
  }

  editButton.addEventListener("click", () => {
    if (editing) {
      restore();
      editing = false;
    } else {
      editing = true;
    }
    applyMode();
  });

  saveButton.addEventListener("click", () => {
    const order = rows()
      .map((row) => row.dataset.videoId || "")
      .filter(Boolean);
    if (!order.length) return;
    $("#order-value").value = order.join(",");
    form.submit();
  });

  list.addEventListener("dragstart", (event) => {
    if (!editing) return;
    const row = event.target.closest(".video-row");
    if (!row) return;
    dragged = row;
    row.classList.add("dragging");
    event.dataTransfer.effectAllowed = "move";
    event.dataTransfer.setData("text/plain", row.dataset.videoId || "");
  });

  list.addEventListener("dragend", () => {
    if (dragged) dragged.classList.remove("dragging");
    dragged = null;
  });

  list.addEventListener("dragover", (event) => {
    if (!editing || !dragged) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "move";
    if (event.clientY < 90) {
      window.scrollBy(0, -14);
    } else if (event.clientY > window.innerHeight - 90) {
      window.scrollBy(0, 14);
    }
    const target = event.target.closest(".video-row");
    if (!target || target === dragged) return;
    const rect = target.getBoundingClientRect();
    const before = event.clientY < rect.top + rect.height / 2;
    list.insertBefore(dragged, before ? target : target.nextSibling);
  });

  list.addEventListener("drop", (event) => {
    if (editing) event.preventDefault();
  });

  // In edit mode a click must not open a video or trigger a row action.
  list.addEventListener("click", (event) => {
    if (!editing) return;
    event.preventDefault();
    event.stopPropagation();
  }, true);

  list.addEventListener("keydown", (event) => {
    if (!editing) return;
    const row = event.target.closest(".video-row");
    if (!row) return;
    if (event.key === "ArrowUp" && row.previousElementSibling) {
      event.preventDefault();
      list.insertBefore(row, row.previousElementSibling);
    } else if (event.key === "ArrowDown" && row.nextElementSibling) {
      event.preventDefault();
      list.insertBefore(row.nextElementSibling, row);
    }
  });
}

export function initNewPlaylistLocation() {
  const select = $("#new-playlist-location");
  if (!select) return;
  let touched = false;
  select.addEventListener("change", () => { touched = true; });
  $$(".pick-row input[type=checkbox]").forEach((box) => {
    box.addEventListener("change", () => {
      if (touched || !box.checked) return;
      const row = box.closest(".pick-row");
      const location = row ? row.dataset.locationId : "";
      if (!location) return;
      const option = Array.from(select.options).find(
        (candidate) => candidate.value === location
      );
      if (option) select.value = location;
    });
  });
}

export function initVideoFilter() {
  const input = $("#video-filter");
  if (!input) return;
  const rows = $$(".pick-row");
  const count = $("#pick-count");
  const total = rows.length;
  const update = () => {
    const query = input.value.trim().toLowerCase();
    let shown = 0;
    rows.forEach((row) => {
      const match = !query
        || String(row.dataset.search || "").includes(query);
      row.hidden = !match;
      if (match) shown += 1;
    });
    if (count) count.textContent = `${shown} of ${total} videos shown`;
  };
  input.addEventListener("input", update);
}

