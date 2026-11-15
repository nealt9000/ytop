import { $, $$, fmt, csrfToken, segmentIndexAt, storageGet, storageSet } from "./core.js";
import { syncAutoplayLinks } from "./ui.js";

const STORAGE_AUTOPLAY = "ytop.autoplay";
const STORAGE_CC = "ytop.cc";
const STORAGE_SPEED = "ytop.speed";

export function initPlayer(data) {
  const video = $("#player");
  const wrap = $("#player-wrap");
  if (!video || !data || !wrap) {
    return;
  }

  const timeline = $("#timeline");
  const played = $("#timeline-played");
  const buffer = $("#timeline-buffer");
  const handle = $("#timeline-handle");
  const tooltip = $("#timeline-tooltip");
  const chaptersEl = $("#timeline-chapters");
  const readout = $("#time-readout");
  const playButton = $("#play-toggle");
  const bigPlay = $("#player-big-play");
  const muteButton = $("#mute-toggle");
  const volume = $("#volume");
  const ccButton = $("#cc-toggle");
  const fullscreenButton = $("#fullscreen-toggle");
  const captions = $("#captions");
  const progressLabel = $("#watch-progress");
  const markButton = $("#mark-watched");
  const autoplayToggle = $("#autoplay-toggle");
  const nextLink = $("#next-link");
  const resumeNote = $("#resume-note");
  const resumeText = $("#resume-text");
  const tipThumb = $("#timeline-tip-thumb");
  const tipTime = $("#timeline-tip-time");
  const tipChapter = $("#timeline-tip-chapter");
  const tipRange = $("#timeline-tip-range");
  const alternateAudio = $("#alternate-audio");
  const audioMenu = $("#audio-menu");
  const audioToggle = $("#audio-toggle");
  const audioPanel = $("#audio-panel");
  const audioCurrent = $("#audio-current");
  const audioTracks = Array.isArray(data.audioTracks) ? data.audioTracks : [];
  const speedSelect = $("#speed-select");

  let duration = Number(data.duration || 0);
  const chapters = Array.isArray(data.chapters) ? data.chapters : [];
  // The transcript is already rendered once in the page. Read caption timing
  // and text from that DOM instead of embedding a second JSON copy.
  const transcriptItems = $$("#transcript li");
  const transcript = transcriptItems.map((item) => ({
    start: Number(item.dataset.start) || 0,
    end: Number(item.dataset.end) || 0,
    text: (item.querySelector(".segment-text") || item).textContent || "",
  }));
  let watched = Boolean(data.watched);
  let saved = Number(data.savedPosition || 0);
  let started = false;
  let lastSave = 0;
  let lastBeacon = 0;
  let dragging = false;
  let resumeWasPlaying = false;
  let activeChapter = -1;
  let activeSegment = -1;
  let hoverChapter = -1;
  const previewCache = new Set();
  const previewPoster = data.posterUrl || "";
  let captionsEnabled = transcript.length > 0 &&
    storageGet(STORAGE_CC, null) !== "0";
  let resumeTimer = 0;
  let activeAudioTrack = 0;
  let userMuted = video.muted;

  // -- playback speed ---------------------------------------------------
  function applyPlaybackRate(rate) {
    const value = Math.max(0.1, Math.min(4, Number(rate) || 1));
    video.defaultPlaybackRate = value;
    video.playbackRate = value;
    if (alternateAudio) alternateAudio.playbackRate = value;
  }

  function storedPlaybackRate() {
    const stored = storageGet(STORAGE_SPEED, null);
    const value = stored === null ? 1 : Number(stored);
    return Number.isFinite(value) && value >= 0.1 && value <= 4 ? value : 1;
  }

  if (speedSelect) {
    const stored = storedPlaybackRate();
    const option = Array.from(speedSelect.options).find(
      (candidate) => Math.abs(Number(candidate.value) - stored) < 0.001
    );
    speedSelect.value = option ? option.value : "1";
    applyPlaybackRate(Number(speedSelect.value));
    speedSelect.addEventListener("change", () => {
      applyPlaybackRate(Number(speedSelect.value));
      storageSet(STORAGE_SPEED, speedSelect.value);
    });
  } else {
    applyPlaybackRate(storedPlaybackRate());
  }

  // -- watched state ----------------------------------------------------
  function setWatched(value) {
    watched = Boolean(value);
    if (!markButton) return;
    markButton.textContent = watched ? "Watched" : "Mark watched";
    markButton.classList.toggle("is-watched", watched);
    markButton.setAttribute("aria-pressed", watched ? "true" : "false");
  }
  setWatched(watched);

  // -- progress ---------------------------------------------------------
  function progressBody(extra = {}) {
    return Object.assign({
      playlist_id: data.playlistId,
      video_id: data.videoId,
      position: Number(video.currentTime || 0),
      duration: duration || Number(video.duration || 0) || null,
    }, extra);
  }

  function postProgress(extra) {
    const payload = progressBody(extra);
    fetch(data.progressUrl, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": csrfToken(),
      },
      body: JSON.stringify(payload),
      keepalive: true,
    }).catch(() => {});
  }

  function save(extra, useBeacon = false) {
    const now = Date.now();
    if (!extra && !useBeacon && now - lastSave < 4000) return;
    lastSave = now;
    postProgress(extra || {});
  }

  // -- playback ---------------------------------------------------------
  function isPlaying() {
    return !video.paused && !video.ended;
  }

  function play() {
    const attempt = video.play();
    if (attempt && attempt.catch) attempt.catch(() => {});
  }

  function selectedAudio() {
    return audioTracks[activeAudioTrack] || null;
  }

  function usingAlternateAudio() {
    const track = selectedAudio();
    return Boolean(track && !track.embedded && track.url && alternateAudio);
  }

  function applyAudioLevels() {
    video.volume = Math.max(0, Math.min(1, Number(video.volume) || 0));
    if (usingAlternateAudio()) {
      video.muted = true;
      alternateAudio.volume = video.volume;
      alternateAudio.muted = userMuted;
    } else {
      video.muted = userMuted;
      if (alternateAudio) alternateAudio.pause();
    }
    if (muteButton) {
      muteButton.classList.toggle("is-muted", userMuted || video.volume === 0);
    }
    if (wrap) wrap.classList.toggle("is-muted", userMuted || video.volume === 0);
  }

  function setAlternateSource(track) {
    if (!alternateAudio) return;
    alternateAudio.pause();
    if (!track || track.embedded || !track.url) {
      alternateAudio.removeAttribute("src");
      delete alternateAudio.dataset.trackId;
      alternateAudio.load();
      return;
    }
    if (alternateAudio.dataset.trackId !== track.id) {
      alternateAudio.dataset.trackId = track.id;
      alternateAudio.src = track.url;
      alternateAudio.load();
    }
  }

  function syncAlternateAudio(force = false) {
    if (!usingAlternateAudio() || !alternateAudio) return;
    if (alternateAudio.playbackRate !== video.playbackRate) {
      alternateAudio.playbackRate = video.playbackRate;
    }
    if (alternateAudio.readyState >= 1) {
      const drift = Math.abs(
        Number(alternateAudio.currentTime || 0) - Number(video.currentTime || 0)
      );
      if (force || drift > 0.35) {
        try { alternateAudio.currentTime = video.currentTime; } catch (_) {}
      }
    }
    if (video.paused || video.ended) {
      if (!alternateAudio.paused) alternateAudio.pause();
    } else if (alternateAudio.paused) {
      const attempt = alternateAudio.play();
      if (attempt && attempt.catch) attempt.catch(() => {});
    }
  }

  function updateAudioUi() {
    if (!audioTracks.length) return;
    const track = selectedAudio();
    if (audioCurrent && track) audioCurrent.textContent = track.display_label || track.label;
    $$(".audio-option").forEach((button) => {
      const active = Number(button.dataset.audioIndex) === activeAudioTrack;
      button.classList.toggle("active", active);
      button.setAttribute("aria-pressed", active ? "true" : "false");
    });
  }

  function closeAudioMenu() {
    if (!audioPanel || !audioToggle) return;
    audioPanel.hidden = true;
    audioToggle.setAttribute("aria-expanded", "false");
  }

  function selectAudioTrack(index) {
    if (!audioTracks[index]) return;
    activeAudioTrack = index;
    const track = audioTracks[index];
    setAlternateSource(track);
    applyAudioLevels();
    updateAudioUi();
    closeAudioMenu();
    if (usingAlternateAudio()) {
      const start = () => {
        try { alternateAudio.currentTime = video.currentTime; } catch (_) {}
        if (!video.paused && !video.ended) {
          const attempt = alternateAudio.play();
          if (attempt && attempt.catch) attempt.catch(() => {});
        }
      };
      if (alternateAudio.readyState >= 1) start();
      else alternateAudio.addEventListener("loadedmetadata", start, { once: true });
    }
  }

  if (audioToggle && audioPanel) {
    audioToggle.addEventListener("click", (event) => {
      event.stopPropagation();
      const open = audioPanel.hidden;
      audioPanel.hidden = !open;
      audioToggle.setAttribute("aria-expanded", open ? "true" : "false");
    });
    audioPanel.addEventListener("click", (event) => event.stopPropagation());
    document.addEventListener("click", closeAudioMenu);
    $$(".audio-option").forEach((button) => {
      button.addEventListener("click", () => {
        selectAudioTrack(Number(button.dataset.audioIndex));
      });
    });
    updateAudioUi();
  }
  if (alternateAudio) {
    alternateAudio.addEventListener("error", () => {
      // A missing alternate must not kill the embedded original audio.
      if (activeAudioTrack !== 0) selectAudioTrack(0);
    });
  }

  function togglePlay() {
    if (video.paused || video.ended) play();
    else video.pause();
  }

  function seekTo(seconds) {
    const target = Math.max(0, Math.min(Number(seconds) || 0, duration || Infinity));
    try { video.currentTime = target; } catch (_) { /* metadata not ready */ }
    syncAlternateAudio(true);
    updateCaptions(target);
    updateChapters(target);
    updateTranscript(target);
  }

  function updatePlayUi() {
    wrap.classList.toggle("is-playing", isPlaying());
    if (playButton) {
      playButton.setAttribute("aria-label", isPlaying() ? "Pause" : "Play");
    }
  }

  function scheduleResumeHide() {
    if (!resumeNote || resumeNote.hidden) return;
    window.clearTimeout(resumeTimer);
    resumeTimer = window.setTimeout(() => { resumeNote.hidden = true; }, 7000);
  }

  video.addEventListener("play", () => {
    updatePlayUi();
    scheduleResumeHide();
    if (usingAlternateAudio() && alternateAudio && alternateAudio.paused) {
      const attempt = alternateAudio.play();
      if (attempt && attempt.catch) attempt.catch(() => {});
    }
  });
  video.addEventListener("pause", () => {
    updatePlayUi();
    if (alternateAudio && !alternateAudio.paused) alternateAudio.pause();
  });
  video.addEventListener("ended", () => {
    updatePlayUi();
    if (alternateAudio) alternateAudio.pause();
  });

  function startPlayback() {
    if (started) return;
    started = true;
    duration = Number(video.duration) || duration;
    buildTimelineChapters();
    updateTimeline();
    updateCaptions(video.currentTime);
    updateChapters(video.currentTime);
    updateTranscript(video.currentTime);
    const wanted = Number(data.startAt || 0);
    if (wanted > 0) {
      seekTo(wanted);
      if (Math.abs(wanted - saved) < 2 && wanted > 5) {
        resumeText.textContent = `Resumed at ${fmt(wanted)}.`;
        resumeNote.hidden = false;
      }
    }
    updateReadout();
    // Clicking a video is a navigation inside the site, so start playback
    // immediately. If the browser blocks it, the large play button remains.
    play();
  }

  video.addEventListener("loadedmetadata", startPlayback);
  video.addEventListener("canplay", startPlayback);
  if (video.readyState >= 1) {
    // The metadata may have arrived before this script ran (cached media).
    startPlayback();
  }

  function updateReadout() {
    if (readout) readout.textContent = `${fmt(video.currentTime)} / ${fmt(duration || video.duration)}`;
    if (progressLabel) {
      progressLabel.textContent = `${fmt(video.currentTime)} / ${fmt(duration || video.duration)}`;
    }
  }

  function updateTimeline() {
    const total = duration || Number(video.duration) || 0;
    if (!total) return;
    const ratio = Math.max(0, Math.min(1, video.currentTime / total));
    if (played) played.style.width = `${ratio * 100}%`;
    if (handle) handle.style.left = `${ratio * 100}%`;
    let buffered = 0;
    if (video.buffered && video.buffered.length) {
      buffered = video.buffered.end(video.buffered.length - 1);
    }
    if (buffer) buffer.style.width = `${Math.max(0, Math.min(1, buffered / total)) * 100}%`;
    if (timeline) {
      timeline.setAttribute("aria-valuenow", Math.round(ratio * 100));
      timeline.setAttribute("aria-valuetext", `${fmt(video.currentTime)} of ${fmt(total)}`);
    }
  }

  function buildTimelineChapters() {
    if (!chaptersEl) return;
    chaptersEl.textContent = "";
    const total = duration || Number(video.duration) || 0;
    if (!total || !chapters.length) return;
    chapters.forEach((chapter, index) => {
      const next = chapters[index + 1];
      const start = Math.max(0, Math.min(Number(chapter.start) || 0, total));
      const end = Math.min(
        total,
        Number(chapter.end) || (next ? Number(next.start) : total) || total
      );
      if (end <= start) return;
      const segment = document.createElement("div");
      segment.className = "timeline-chapter" + (index % 2 ? " odd" : "");
      segment.style.left = `${(start / total) * 100}%`;
      segment.style.width = `${((end - start) / total) * 100}%`;
      segment.title = `${fmt(start)} ${chapter.title}`;
      segment.dataset.index = String(index);
      chaptersEl.appendChild(segment);
    });
  }

  function chapterIndexAt(time) {
    let index = -1;
    for (let i = 0; i < chapters.length; i += 1) {
      if (Number(chapters[i].start) <= time) index = i;
      else break;
    }
    return index;
  }

  function updateChapters(time) {
    const index = chapterIndexAt(time);
    if (index === activeChapter) return;
    activeChapter = index;
    const items = $$("#chapter-list li");
    items.forEach((item, i) => item.classList.toggle("active", i === index));
    $$(".timeline-chapter", chaptersEl || document).forEach((segment, i) => {
      segment.classList.toggle("active", i === index);
    });
    prefetchPreview(index);
    prefetchPreview(index + 1);
  }

  // -- timeline interaction --------------------------------------------
  function ratioFromEvent(event) {
    const rect = timeline.getBoundingClientRect();
    return Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width));
  }

  function timeFromEvent(event) {
    return ratioFromEvent(event) * (duration || Number(video.duration) || 0);
  }

  function previewSrc(seconds) {
    if (!data.previewBase) return "";
    const whole = Math.max(0, Math.floor(Number(seconds) || 0));
    return `${data.previewBase}${whole}`;
  }

  function chapterPreviewSeconds(index) {
    const range = chapterRange(index, duration || Number(video.duration) || 0);
    if (!range) return 0;
    // One second in, but never past the chapter into the next one.
    return Math.max(
      range.start,
      Math.min(range.start + 1, Math.max(range.start, range.end - 0.1))
    );
  }

  function prefetchPreview(index) {
    if (index < 0 || index >= chapters.length) return;
    const url = previewSrc(chapterPreviewSeconds(index));
    if (!url || previewCache.has(url)) return;
    previewCache.add(url);
    const image = new Image();
    image.src = url;
  }

  function setTipImage(url) {
    if (!tipThumb) return;
    if (!url) {
      tipThumb.hidden = true;
      return;
    }
    tipThumb.hidden = false;
    if (tipThumb.dataset.src === url) return;
    tipThumb.dataset.src = url;
    tipThumb.src = url;
  }

  function chapterRange(index, total) {
    const chapter = index >= 0 ? chapters[index] : null;
    if (!chapter) return null;
    const start = Number(chapter.start) || 0;
    const next = chapters[index + 1];
    const end = Number(chapter.end)
      || (next ? Number(next.start) : total)
      || total;
    return { start, end, chapter };
  }

  function positionTooltip(event) {
    const rect = timeline.getBoundingClientRect();
    const x = Math.max(0, Math.min(rect.width, event.clientX - rect.left));
    const width = tooltip.offsetWidth || 260;
    const half = Math.min(width / 2, rect.width / 2);
    tooltip.style.left = `${Math.max(half, Math.min(rect.width - half, x))}px`;
  }

  function hideTooltip() {
    if (tooltip) tooltip.hidden = true;
    hoverChapter = -1;
    $$(".timeline-chapter", chaptersEl || document).forEach((segment) => {
      segment.classList.remove("hover");
    });
  }

  function showTooltip(event) {
    if (!tooltip || !timeline) return;
    const total = duration || Number(video.duration) || 0;
    if (!total) return;
    const ratio = ratioFromEvent(event);
    const time = ratio * total;
    const index = chapterIndexAt(time);
    const range = chapterRange(index, total);
    tooltip.hidden = false;
    if (tipTime) tipTime.textContent = fmt(time);
    if (tipChapter) {
      tipChapter.textContent = range ? range.chapter.title : "";
      tipChapter.hidden = !range;
    }
    if (tipRange) {
      tipRange.textContent = range
        ? `${fmt(range.start)}–${fmt(range.end)}`
        : "";
      tipRange.hidden = !range;
    }
    const previewTime = range
      ? chapterPreviewSeconds(index)
      : Math.floor(time / 15) * 15;
    const preview = previewSrc(previewTime);
    setTipImage(preview || previewPoster);
    if (index !== hoverChapter) {
      hoverChapter = index;
      $$(".timeline-chapter", chaptersEl || document).forEach((segment, i) => {
        segment.classList.toggle("hover", i === index);
      });
      prefetchPreview(index);
    }
    // Measure after the text is in place so the tooltip stays on-screen.
    positionTooltip(event);
  }

  if (tipThumb) {
    tipThumb.addEventListener("error", () => {
      const current = tipThumb.dataset.src;
      if (previewPoster && current !== previewPoster) {
        tipThumb.dataset.src = previewPoster;
        tipThumb.src = previewPoster;
      } else {
        tipThumb.hidden = true;
      }
    });
  }

  if (timeline) {
    timeline.addEventListener("pointerdown", (event) => {
      dragging = true;
      resumeWasPlaying = isPlaying();
      timeline.setPointerCapture?.(event.pointerId);
      seekTo(timeFromEvent(event));
      showTooltip(event);
    });
    timeline.addEventListener("pointermove", (event) => {
      showTooltip(event);
      if (dragging) seekTo(timeFromEvent(event));
    });
    timeline.addEventListener("pointerup", (event) => {
      if (!dragging) return;
      dragging = false;
      timeline.releasePointerCapture?.(event.pointerId);
      if (resumeWasPlaying && video.paused) play();
      save();
    });
    timeline.addEventListener("pointerleave", hideTooltip);
    timeline.addEventListener("pointercancel", (event) => {
      dragging = false;
      timeline.releasePointerCapture?.(event.pointerId);
      hideTooltip();
    });
    timeline.addEventListener("keydown", (event) => {
      if (event.key === "ArrowRight") {
        event.preventDefault();
        seekTo(video.currentTime + 5);
      } else if (event.key === "ArrowLeft") {
        event.preventDefault();
        seekTo(video.currentTime - 5);
      }
    });
  }

  function updateCaptions(time) {
    if (!captions) return;
    if (!captionsEnabled || !transcript.length) {
      captions.hidden = true;
      return;
    }
    const index = segmentIndexAt(transcript, time + 0.12, (item) => item.start);
    let text = "";
    if (index >= 0) {
      const segment = transcript[index];
      if (time <= Number(segment.end) + 0.35) text = segment.text;
    }
    if (!text) {
      captions.hidden = true;
      captions.textContent = "";
      return;
    }
    captions.textContent = text;
    captions.hidden = false;
  }

  function updateTranscript(time) {
    if (!transcriptItems.length) return;
    const index = segmentIndexAt(
      transcriptItems,
      time + 0.25,
      (item) => Number(item.dataset.start) || 0
    );
    if (index === activeSegment) return;
    if (activeSegment >= 0 && transcriptItems[activeSegment]) {
      transcriptItems[activeSegment].classList.remove("active");
    }
    activeSegment = index;
    if (index >= 0) {
      const item = transcriptItems[index];
      item.classList.add("active");
      const panel = item.closest(".tab-panel");
      if (panel && !panel.hidden && follow) {
        item.scrollIntoView({ block: "nearest" });
      }
    }
  }

  // timeupdate fires ~4x/s; the visual work only needs one paint per frame.
  let visualFrame = 0;
  function scheduleVisualUpdate() {
    if (visualFrame) return;
    visualFrame = window.requestAnimationFrame(() => {
      visualFrame = 0;
      const time = video.currentTime;
      updateReadout();
      updateTimeline();
      updateCaptions(time);
      updateChapters(time);
      updateTranscript(time);
    });
  }

  video.addEventListener("timeupdate", () => {
    scheduleVisualUpdate();
    syncAlternateAudio();
    if (!video.paused && Date.now() - lastSave > 5000) save();
  });

  video.addEventListener("seeked", () => {
    updateTimeline();
    updateCaptions(video.currentTime);
    updateChapters(video.currentTime);
    updateTranscript(video.currentTime);
    syncAlternateAudio(true);
    save();
  });

  video.addEventListener("ended", () => {
    setWatched(true);
    postProgress({ position: duration || video.duration || 0, watched: true });
    const autoplay = autoplayToggle ? autoplayToggle.checked : false;
    if (autoplay && data.nextUrl) {
      window.location.href = `${data.nextUrl}?autoplay=1`;
    }
  });

  // -- controls ---------------------------------------------------------
  if (playButton) playButton.addEventListener("click", togglePlay);
  if (bigPlay) bigPlay.addEventListener("click", togglePlay);
  video.addEventListener("click", togglePlay);
  video.addEventListener("dblclick", (event) => {
    event.preventDefault();
    toggleFullscreen();
  });

  if (muteButton) {
    muteButton.addEventListener("click", () => {
      userMuted = !userMuted;
      if (!userMuted && video.volume === 0) video.volume = 0.5;
      applyAudioLevels();
      storageSet("ytop.volume", String(video.volume));
    });
  }
  if (volume) {
    const stored = storageGet("ytop.volume", null);
    if (stored !== null) {
      const value = Number(stored);
      if (Number.isFinite(value)) video.volume = Math.max(0, Math.min(1, value));
    }
    volume.value = String(video.volume);
    userMuted = video.muted || video.volume === 0;
    applyAudioLevels();
    volume.addEventListener("input", () => {
      video.volume = Number(volume.value);
      userMuted = video.volume === 0;
      applyAudioLevels();
      storageSet("ytop.volume", String(video.volume));
    });
  }
  video.addEventListener("volumechange", () => {
    if (usingAlternateAudio()) {
      if (alternateAudio) {
        alternateAudio.volume = video.volume;
        alternateAudio.muted = userMuted;
      }
    }
    if (muteButton) muteButton.classList.toggle("is-muted", userMuted || video.volume === 0);
    wrap.classList.toggle("is-muted", userMuted || video.volume === 0);
  });

  function toggleFullscreen() {
    if (document.fullscreenElement) {
      document.exitFullscreen?.();
    } else {
      wrap.requestFullscreen?.();
    }
  }
  if (fullscreenButton) {
    fullscreenButton.addEventListener("click", toggleFullscreen);
  }
  document.addEventListener("fullscreenchange", () => {
    const full = Boolean(document.fullscreenElement);
    if (fullscreenButton) {
      fullscreenButton.setAttribute("aria-label", full ? "Exit fullscreen" : "Fullscreen");
    }
  });

  if (ccButton) {
    ccButton.classList.toggle("active", captionsEnabled);
    ccButton.setAttribute("aria-pressed", captionsEnabled ? "true" : "false");
    ccButton.addEventListener("click", () => {
      captionsEnabled = !captionsEnabled;
      storageSet(STORAGE_CC, captionsEnabled ? "1" : "0");
      ccButton.classList.toggle("active", captionsEnabled);
      ccButton.setAttribute("aria-pressed", captionsEnabled ? "true" : "false");
      updateCaptions(video.currentTime);
    });
  }

  // -- watched / autoplay ----------------------------------------------
  if (markButton) {
    markButton.setAttribute("aria-pressed", watched ? "true" : "false");
    markButton.addEventListener("click", () => {
      const next = !watched;
      setWatched(next);
      postProgress({ position: video.currentTime, watched: next });
    });
  }

  if (autoplayToggle) {
    const stored = storageGet(STORAGE_AUTOPLAY, null);
    if (stored !== null) autoplayToggle.checked = stored === "1";
    else autoplayToggle.checked = Boolean(data.autoplay);
    autoplayToggle.addEventListener("change", () => {
      storageSet(STORAGE_AUTOPLAY, autoplayToggle.checked ? "1" : "0");
      syncAutoplayLinks();
    });
    syncAutoplayLinks();
  } else if (nextLink) {
    syncAutoplayLinks();
  }

  if (resumeNote) {
    const startOver = $("#resume-start-over");
    const dismiss = $("#resume-dismiss");
    if (startOver) startOver.addEventListener("click", () => {
      seekTo(0);
      saved = 0;
      resumeNote.hidden = true;
      postProgress({ position: 0 });
      play();
    });
    if (dismiss) dismiss.addEventListener("click", () => { resumeNote.hidden = true; });
  }

  // -- shared seek targets ---------------------------------------------
  document.addEventListener("click", (event) => {
    const target = event.target.closest("[data-seek]");
    if (!target || target.closest(".ext")) return;
    event.preventDefault();
    const seconds = Number(target.dataset.seek);
    if (!Number.isFinite(seconds)) return;
    seekTo(seconds);
    play();
  });

  // -- transcript filter ------------------------------------------------
  const filter = $("#transcript-filter");
  const transcriptCount = $("#transcript-count");
  if (filter) {
    filter.addEventListener("input", () => {
      const needle = filter.value.trim().toLowerCase();
      let shown = 0;
      transcriptItems.forEach((item) => {
        const hit = !needle || (item.dataset.text || "").includes(needle);
        item.classList.toggle("hidden", !hit);
        if (hit) shown += 1;
      });
      if (transcriptCount) {
        transcriptCount.textContent = `${shown} line${shown === 1 ? "" : "s"}`;
      }
    });
  }
  const transcriptScroll = $("#transcript-scroll");
  let follow = true;
  let followTimer = 0;
  if (transcriptScroll) {
    transcriptScroll.addEventListener("scroll", () => {
      follow = false;
      window.clearTimeout(followTimer);
      followTimer = window.setTimeout(() => { follow = true; }, 6000);
    }, { passive: true });
  }

  // -- page lifecycle ---------------------------------------------------
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") save({}, true);
  });
  window.addEventListener("pagehide", () => {
    const now = Date.now();
    if (now - lastBeacon > 1000) {
      lastBeacon = now;
      postProgress({}, true);
    }
  });

  // -- keyboard ---------------------------------------------------------
  document.addEventListener("keydown", (event) => {
    const tag = (event.target.tagName || "").toLowerCase();
    if (["input", "textarea", "select", "button", "a"].includes(tag)) return;
    if (event.key === " " || event.code === "Space") {
      event.preventDefault();
      togglePlay();
    } else if (event.key === "ArrowRight") {
      event.preventDefault();
      seekTo(video.currentTime + 5);
    } else if (event.key === "ArrowLeft") {
      event.preventDefault();
      seekTo(video.currentTime - 5);
    } else if (event.key === "n" && data.nextUrl) {
      window.location.href = `${data.nextUrl}?autoplay=${autoplayToggle && autoplayToggle.checked ? 1 : 0}`;
    } else if (event.key === "p" && data.prevUrl) {
      window.location.href = `${data.prevUrl}?autoplay=${autoplayToggle && autoplayToggle.checked ? 1 : 0}`;
    } else if (event.key === "c" && ccButton) {
      ccButton.click();
    } else if (event.key === "f") {
      toggleFullscreen();
    } else if (event.key === "m" && muteButton) {
      muteButton.click();
    }
  });

  // -- transcript follow after filtering -------------------------------
  updatePlayUi();
}
