import { $, parseWatchData } from "./js/core.js";
import { initNav, initAddMenu, initDialogs, initTabs } from "./js/ui.js";
import { initPlayer } from "./js/player.js";
import {
  initQueue,
  initReorder,
  initNewPlaylistLocation,
  initVideoFilter,
  initPlaylistFilter,
} from "./js/library.js";
import { restoreScroll, initLiveRefresh, initDownloadsLive } from "./js/live.js";
import { initLifecycle } from "./js/lifecycle.js";
import { initBrowse } from "./js/browse.js";

const data = parseWatchData();

function init() {
  restoreScroll();
  initNav();
  initAddMenu();
  initDialogs();
  initTabs();
  initReorder();
  initNewPlaylistLocation();
  initVideoFilter();
  initBrowse();
  initPlaylistFilter();
  initPlayer(data);
  initQueue();
  initLiveRefresh();
  initDownloadsLive();
  initLifecycle();
  if (window.location.hash === "#transcript") {
    const tab = $('.tab[data-tab="transcript"]');
    if (tab) tab.click();
  }
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init);
} else {
  init();
}
