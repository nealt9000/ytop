const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

export { $, $$ };

export function fmt(value) {
  const total = Math.max(0, Math.floor(Number(value) || 0));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const pad = (n) => String(n).padStart(2, "0");
  return h ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
}

export function fmtBytes(value) {
  const size = Math.max(0, Number(value) || 0);
  if (!size) return "0 B";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  const index = Math.min(
    units.length - 1,
    Math.floor(Math.log(size) / Math.log(1024))
  );
  const scaled = size / 1024 ** index;
  const digits = scaled >= 100 || index === 0 ? 0 : 1;
  return `${scaled.toFixed(digits)} ${units[index]}`;
}

export function csrfToken() {
  const meta = document.querySelector('meta[name="csrf-token"]');
  return meta ? meta.getAttribute("content") || "" : "";
}

// Binary search over a list ordered by start time. Returns the index of the
// last segment that starts at or before ``time`` (or -1).
export function segmentIndexAt(items, time, startOf) {
  let low = 0;
  let high = items.length - 1;
  let found = -1;
  while (low <= high) {
    const middle = (low + high) >> 1;
    if (startOf(items[middle]) <= time) {
      found = middle;
      low = middle + 1;
    } else {
      high = middle - 1;
    }
  }
  return found;
}

export function parseWatchData() {
  const node = $("#watch-data");
  if (!node) return null;
  try {
    return JSON.parse(node.textContent || "{}");
  } catch (error) {
    console.warn("could not parse watch data", error);
    return null;
  }
}

// localStorage can be missing or throw (private windows, blocked site data,
// file quotas). These never throw: a read falls back, a write is dropped.
export function storageGet(key, fallback = null) {
  try {
    const value = window.localStorage.getItem(key);
    return value === null ? fallback : value;
  } catch (error) {
    return fallback;
  }
}

export function storageSet(key, value) {
  try {
    window.localStorage.setItem(key, String(value));
    return true;
  } catch (error) {
    return false;
  }
}
