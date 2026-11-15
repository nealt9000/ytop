import { $, csrfToken } from "./core.js";

// Portable mode: the server exits once no page has sent a heartbeat for a
// while, and Quit stops it so the stick can be unplugged.  An open page
// beats whether or not it is visible: a background tab still counts as
// "a tab is open" (hidden tabs may be throttled to about one beat a
// minute, well inside the idle timeout).
const HEARTBEAT_MS = 30000;

let reloadingStaleTab = false;

function mayReloadStaleTab() {
  // At most one reload a minute, so a refusal with another cause can
  // never turn into a reload loop.
  try {
    const last = Number(window.sessionStorage.getItem("ytop-stale-reload") || 0);
    if (Date.now() - last < 60000) return false;
    window.sessionStorage.setItem("ytop-stale-reload", String(Date.now()));
  } catch (_) {
    return false;
  }
  return true;
}

function heartbeat() {
  fetch("/api/heartbeat", {
    method: "POST",
    headers: { "X-CSRF-Token": csrfToken() },
    keepalive: true,
  }).then(async (response) => {
    // A tab left open from an earlier run holds that run's token: the
    // restarted server refuses it, so fetch the page again (once).  Any
    // other refusal (origin, cross-site) is not fixed by reloading.
    if (response.status !== 403 || reloadingStaleTab) return;
    const body = await response.json().catch(() => ({}));
    if (!String(body.detail || "").includes("CSRF token")) return;
    if (!mayReloadStaleTab()) return;
    reloadingStaleTab = true;
    window.location.reload();
  }).catch(() => {});
}

function askConfirm(prompt, action) {
  const dialog = $("#confirm-dialog");
  const message = $("#confirm-message");
  if (!dialog || !message || typeof dialog.showModal !== "function") {
    return Promise.resolve(window.confirm(prompt));
  }
  const accept = $("#confirm-accept");
  if (accept) accept.textContent = action;
  message.textContent = prompt;
  dialog.returnValue = "";
  return new Promise((resolve) => {
    const decide = () => {
      dialog.removeEventListener("close", decide);
      resolve(dialog.returnValue === "confirm");
    };
    dialog.addEventListener("close", decide);
    dialog.showModal();
  });
}

async function quit(button, force) {
  const body = new URLSearchParams();
  if (force) body.set("force", "1");
  let response;
  try {
    response = await fetch("/api/quit", {
      method: "POST",
      headers: { "X-CSRF-Token": csrfToken() },
      body,
    });
  } catch (error) {
    button.disabled = false;
    return;
  }
  if (response.status === 409) {
    const data = await response.json().catch(() => ({}));
    const count = Number(data.active_jobs) || 0;
    const prompt =
      `${count} download${count === 1 ? " is" : "s are"} still running. ` +
      "Quit anyway? They will stop; add the playlist again later to " +
      "resume the partial downloads.";
    if (await askConfirm(prompt, "Quit anyway")) {
      await quit(button, true);
    } else {
      button.disabled = false;
    }
    return;
  }
  if (!response.ok) {
    button.disabled = false;
    return;
  }
  // The server is stopping: show its static "safe to unplug" page.
  const html = await response.text();
  document.open();
  document.write(html);
  document.close();
}

export function initLifecycle() {
  const form = $("form[data-quit]");
  if (!form) return; // not portable mode
  heartbeat();
  window.setInterval(heartbeat, HEARTBEAT_MS);
  document.addEventListener("visibilitychange", heartbeat);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    event.stopPropagation();
    const button = form.querySelector("button");
    if (button) button.disabled = true;
    quit(button || form, false);
  });
}
