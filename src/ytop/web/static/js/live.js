import { $, $$, fmtBytes } from "./core.js";

// -- live updates ---------------------------------------------------------
function scrollKey() {
  return `ytop.scroll:${window.location.pathname}`;
}

function storeScroll() {
  window.sessionStorage.setItem(scrollKey(), String(window.scrollY));
}

export function restoreScroll() {
  const stored = window.sessionStorage.getItem(scrollKey());
  if (stored === null) return;
  window.sessionStorage.removeItem(scrollKey());
  const y = Number(stored);
  if (Number.isFinite(y) && y > 0) window.scrollTo(0, y);
}

function reloadOnce() {
  storeScroll();
  window.location.reload();
}

// Replace a section with the server-rendered version of the same page while
// keeping the scroll position and, where possible, keyboard focus.
async function softReplaceSection(selector) {
  const current = $(selector);
  if (!current) return false;
  const response = await fetch(window.location.href, {
    cache: "no-store",
    headers: { "X-Requested-With": "fetch" },
  });
  if (!response.ok) return false;
  const html = await response.text();
  const doc = new DOMParser().parseFromString(html, "text/html");
  const incoming = doc.querySelector(selector);
  if (!incoming) return false;
  const active = document.activeElement;
  const focusId = active && active.id ? active.id : "";
  const focusJob = active && active.closest && active.closest("[data-job-id]");
  const focusJobId = focusJob ? focusJob.dataset.jobId : "";
  const y = window.scrollY;
  current.replaceWith(incoming);
  if (doc.body.dataset.liveState) {
    document.body.dataset.liveState = doc.body.dataset.liveState;
  }
  if (doc.body.dataset.liveJob) {
    document.body.dataset.liveJob = doc.body.dataset.liveJob;
  }
  let target = null;
  if (focusJobId) {
    target = $(`[data-job-id="${CSS.escape(focusJobId)}"] button`);
  } else if (focusId) {
    target = document.getElementById(focusId);
  }
  if (target && target.focus) target.focus({ preventScroll: true });
  window.scrollTo(0, y);
  return true;
}

function statusWord(job) {
  if (job.cancel_requested) return job.kind === "move" ? "Cancelling move…" : "Cancelling…";
  if (job.kind === "make_stick") {
    const upgrade = job.stick_mode === "upgrade";
    return {
      queued: "Stick queued",
      running: upgrade ? "Upgrading stick" : "Making stick",
      done: upgrade ? "Stick upgraded" : "Stick ready",
      cancelled: "Stick cancelled",
      failed: "Stick failed",
    }[job.status] || "Stick";
  }
  if (job.kind === "update") {
    return { running: "Updating", done: "Updated" }[job.status] || "Update failed";
  }
  if (job.kind === "move") {
    return {
      queued: "Move queued",
      running: "Moving playlist",
      done: "Move complete",
      cancelled: "Move cancelled",
      failed: "Move failed",
      ready: "Move ready",
      probing: "Preparing move",
    }[job.status] || "Move";
  }
  return {
    probing: "Reading playlist metadata",
    ready: "Ready to download",
    queued: "Queued",
    running: "Downloading",
    done: "Complete",
    cancelled: "Cancelled",
    failed: "Failed",
    paused_offline: "Paused — library offline",
  }[job.status] || job.status;
}

function metricsText(job) {
  if (job.kind === "update") {
    if (job.active) return job.current_title || "";
    return [job.notice, job.error].filter(Boolean).join(" · ");
  }
  const text = baseMetricsText(job);
  if (!job.notice) return text;
  return text ? `${text} · ${job.notice}` : job.notice;
}

function baseMetricsText(job) {
  if (job.kind === "move" || job.kind === "make_stick") {
    if (job.status === "done") {
      const verb = job.kind === "move" ? "moved" : "written";
      let text = `${fmtBytes(job.move_total_bytes)} ${verb}`;
      if (job.move_total_files) text += ` · ${job.move_total_files} files`;
      return text;
    }
    if (job.active) {
      let text = `${fmtBytes(job.move_bytes)} of ${fmtBytes(job.move_total_bytes)}`;
      if (job.move_total_files) {
        text += ` · ${job.move_files}/${job.move_total_files} files`;
      }
      if (job.current_title) text += ` · ${job.current_title}`;
      return text;
    }
    return job.error || "";
  }
  if (job.status === "running" || job.status === "queued") {
    let text = `${job.downloaded || 0} new · ${job.skipped || 0} on disk`;
    if (job.failed) text += ` · ${job.failed} failed`;
    text += ` · ${fmtBytes(job.bytes_downloaded)}`;
    if (job.current_title && job.status === "running") {
      text += ` · ${job.current_title}`;
      if (job.current_total) {
        text += ` (${Math.round((job.current_bytes / job.current_total) * 100)}%)`;
      }
    }
    return text;
  }
  if (job.status === "done") {
    let text = `${job.downloaded || 0} new · ${job.skipped || 0} already on disk`;
    if (job.failed) text += ` · ${job.failed} failed`;
    return text;
  }
  if (job.status === "cancelled" || job.status === "failed" || job.status === "paused_offline") {
    return job.error || "";
  }
  if (job.status === "probing") {
    let text = `${job.probe_done || 0}`;
    if (job.video_count) text += ` of ${job.video_count}`;
    text += " checked";
    if (job.current_title) text += ` · ${job.current_title}`;
    return text;
  }
  if (job.status === "ready") {
    return `Efficient ${fmtBytes(job.estimate_bytes)} · native ${fmtBytes(job.estimate_native_bytes)}`;
  }
  return "";
}

function setBar(root, percent) {
  const ratio = Math.max(0, Math.min(1, Number(percent) || 0));
  const fill = $("[data-job-fill], [data-live-bar-fill]", root);
  if (fill) fill.style.width = `${ratio * 100}%`;
  const bar = $("[data-job-bar], [data-live-bar]", root);
  if (bar) bar.setAttribute("aria-valuenow", String(Math.round(ratio * 100)));
}

function updateJobHooks(job) {
  // Scope to the page content. The <body> also carries live job state
  // attributes, and writing textContent there would replace the whole UI.
  const main = $("#main-content") || document;
  setBar(main, job.active || job.status === "done" ? job.percent : 0);
  const state = $("[data-live-status]", main);
  if (state) state.textContent = statusWord(job);
  const progress = $("[data-live-progress]", main);
  if (progress) progress.textContent = metricsText(job);
}

export function initLiveRefresh() {
  const jobId = document.body.dataset.liveJob;
  if (!jobId) return;
  const page = document.body.dataset.livePage || "";
  let lastStatus = document.body.dataset.liveState || "";
  let stopped = false;
  let inFlight = false;

  async function tick() {
    if (stopped || inFlight || document.visibilityState === "hidden") return;
    inFlight = true;
    try {
      const response = await fetch(
        `/api/jobs/${encodeURIComponent(jobId)}`,
        { cache: "no-store" }
      );
      if (!response.ok) return;
      const job = await response.json();
      updateJobHooks(job);
      const changed = job.status !== lastStatus;
      lastStatus = job.status;
      if (changed && (page === "playlist" || page === "library")) {
        stopped = true;
        reloadOnce();
        return;
      }
      if (changed && (page === "add" || page === "move")) {
        await softReplaceSection("#main-content");
        if (!job.active) {
          stopped = true;
        } else {
          document.body.dataset.liveState = job.status;
        }
      }
      if (!job.active && page !== "add" && page !== "move") stopped = true;
    } catch (_) {
      // A dropped poll is harmless; the next tick tries again.
    } finally {
      inFlight = false;
    }
  }

  tick();
  window.setInterval(tick, 2000);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") tick();
  });
}

function updateDownloadCard(card, job) {
  card.dataset.status = job.status;
  card.dataset.active = job.active ? "1" : "0";
  setBar(card, job.active || job.status === "done" ? job.percent : 0);
  const state = $("[data-job-state]", card);
  if (state) state.textContent = statusWord(job);
  const metrics = $("[data-job-metrics]", card);
  if (metrics) metrics.textContent = metricsText(job);
}

export function initDownloadsLive() {
  if (!$("#downloads-content")) return;
  let inFlight = false;

  async function tick() {
    const root = $("#downloads-content");
    if (!root || inFlight || document.visibilityState === "hidden") return;
    inFlight = true;
    try {
      const response = await fetch("/api/jobs", { cache: "no-store" });
      if (!response.ok) return;
      const payload = await response.json();
      const jobs = Array.isArray(payload.jobs) ? payload.jobs : [];
      const cards = new Map(
        $$(".job-card", root).map((card) => [card.dataset.jobId, card])
      );
      let structural = jobs.length !== cards.size;
      const finished = [];
      jobs.forEach((job) => {
        const card = cards.get(job.id);
        if (!card) {
          structural = true;
          return;
        }
        if (card.dataset.status !== job.status) {
          structural = true;
          if (!job.active) finished.push(job);
        }
        updateDownloadCard(card, job);
      });
      const announcer = $("#downloads-announcer");
      if (finished.length && announcer) {
        announcer.textContent = finished
          .map((job) => `${job.title || job.url}: ${statusWord(job)}`)
          .join(". ");
      }
      if (structural) await softReplaceSection("#downloads-content");
    } catch (_) {
      // Keep the last known state; the next poll retries.
    } finally {
      inFlight = false;
    }
  }

  tick();
  window.setInterval(tick, 2000);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") tick();
  });
}
