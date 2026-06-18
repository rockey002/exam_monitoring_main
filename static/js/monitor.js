async function sendEvent(type, details) {
  let severity = "warning";
  if (
    type === "loud_noise" ||
    type === "unwanted_talk" ||
    type === "alt_tab_suspected" ||
    type === "restricted_shortcut" ||
    type === "fullscreen_exit"
  ) {
    severity = "critical";
  }
  if (
    type === "mic_access_granted" ||
    type === "mic_enabled_by_admin" ||
    type === "mic_disabled_by_admin"
  ) {
    severity = "info";
  }
  try {
    await fetch("/event", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ type, details, severity }),
      keepalive: true,
    });
  } catch (e) {
    // Ignore network errors; monitoring should not block the exam.
  }
}

const focusStatus = document.getElementById("focus-status");
const micStatus = document.getElementById("mic-status");
const micLevelFill = document.getElementById("mic-level-fill");
const examForm = document.getElementById("exam-form");
const timerLabel = document.getElementById("timer-label");
const timeTakenField = document.getElementById("time_taken_seconds");
const questionPagerStatus = document.getElementById("question-pager-status");
const questionPageCount = document.getElementById("question-page-count");
const prevQuestionPageBtn = document.getElementById("prev-question-page");
const nextQuestionPageBtn = document.getElementById("next-question-page");
const examSubmitFooter = document.getElementById("exam-submit-footer");
const warningList = document.getElementById("live-warning-list");
const browserWarningBanner = document.getElementById("browser-warning-banner");
const browserProcess = document.getElementById("browser-process");
const videoProcess = document.getElementById("video-process");
const audioProcess = document.getElementById("audio-process");
const warningCountLabel = document.getElementById("warning-count-label");
let micInitialized = false;
let micStream = null;
let micAudioCtx = null;
let micTickTimer = null;
let micLoopActive = false;
let micAllowedByAdmin = true;
const examStartedAtMs = Date.now();
let examAutoSubmitted = false;
let autoStopEnabled = false;
let autoStopArmedAtMs = examStartedAtMs;
let autoStopWarningLimit = 5;
let autoStopCriticalLimit = 2;
let tabSwitchLimit = 1;
let tabSwitchCount = 0;
let lastTabViolationAt = 0;
let lastShortcutViolationAt = 0;
let maxWarningsBannerCount = 0;
const RECENT_WARNING_WINDOW_MS = 30000;
const MAX_VISIBLE_WARNINGS = 6;
const QUESTIONS_PER_PAGE = 5;
const AUDIO_EVENT_TYPES = new Set([
  "voice_activity",
  "unwanted_talk",
  "background_noise",
  "loud_noise",
  "mic_access_denied",
  "mic_access_granted",
  "mic_disabled_by_admin",
  "mic_enabled_by_admin",
]);
const BROWSER_EVENT_TYPES = new Set([
  "tab_hidden",
  "tab_switch_or_window_blur",
  "tab_close_or_navigation",
  "restricted_shortcut",
  "alt_tab_suspected",
  "fullscreen_exit",
]);
const VIDEO_EVENT_TYPES = new Set([
  "multiple_faces",
  "face_absent",
  "eyes_not_visible",
  "eye_looking_away",
  "looking_away",
  "head_movement",
]);
const AUTO_STOP_EVENTS = new Set([
  "multiple_faces",
  "face_absent",
  "eyes_not_visible",
  "eye_looking_away",
  "looking_away",
  "head_movement",
  "unwanted_talk",
  "loud_noise",
  "tab_hidden",
  "tab_switch_or_window_blur",
  "restricted_shortcut",
  "alt_tab_suspected",
  "fullscreen_exit",
]);

function updateFocusLabel(text) {
  if (focusStatus) {
    focusStatus.textContent = text;
  }
}

function setText(el, text) {
  if (el) el.textContent = text;
}

function eventTimestampMs(item) {
  if (!item || !item.created_at || item.created_at === "LIVE") return Date.now();
  const parsed = Date.parse(`${item.created_at}Z`);
  return Number.isNaN(parsed) ? 0 : parsed;
}

function isWarningEvent(item) {
  return item && item.event_type && (item.severity || "warning") !== "info";
}

function recentWarningItems(items = []) {
  const now = Date.now();
  return items.filter((item) => {
    if (!isWarningEvent(item)) return false;
    if (item.created_at === "LIVE") return true;
    const createdMs = eventTimestampMs(item);
    return createdMs > 0 && now - createdMs <= RECENT_WARNING_WINDOW_MS;
  });
}

function sortWarningsNewestFirst(items = []) {
  return items.slice().sort((a, b) => eventTimestampMs(b) - eventTimestampMs(a));
}

function updateMonitoringSummary(items = []) {
  const activeItems = recentWarningItems(items);
  setText(
    warningCountLabel,
    activeItems.length === 0 ? "No warnings" : `${activeItems.length} warning(s)`
  );

  const browserEvents = activeItems.filter((item) => classifyWarningType(item.event_type, item.source) === "Browser");
  const videoEvents = activeItems.filter((item) => ["Face", "Eye", "Head", "Video"].includes(classifyWarningType(item.event_type, item.source)));
  const audioEvents = activeItems.filter((item) => classifyWarningType(item.event_type, item.source) === "Audio");

  const browserText = browserEvents[0]
    ? formatWarningType(browserEvents[0].event_type)
    : "Active";
  const videoText = videoEvents[0]
    ? formatWarningType(videoEvents[0].event_type)
    : "Camera active";
  const audioText = audioEvents[0]
    ? formatWarningType(audioEvents[0].event_type)
    : `${micInitialized ? "Active" : "Waiting"}`;

  setText(browserProcess, browserText);
  setText(videoProcess, videoText);
  setText(audioProcess, audioText);
}

function showBrowserWarning(message, level = "warning") {
  if (!browserWarningBanner) return;
  browserWarningBanner.textContent = message;
  browserWarningBanner.className = `browser-warning-banner show ${level}`;
  window.clearTimeout(showBrowserWarning.dismissTimer);
  showBrowserWarning.dismissTimer = window.setTimeout(() => {
    if (browserWarningBanner) {
      browserWarningBanner.className = "browser-warning-banner";
    }
  }, 3600);
}

function updateMicStatus(text) {
  if (!micStatus) return;
  const micText = micStatus.querySelector('.mic-text');
  if (micText) {
    micText.textContent = text;
  }
  
  // Update icon state
  micStatus.classList.remove('active', 'denied');
  if (text === 'active') {
    micStatus.classList.add('active');
  } else if (text === 'denied' || text.includes('disabled')) {
    micStatus.classList.add('denied');
  }
}

function updateMicLevel(rms) {
  // Mic level meter removed - using icon-based status instead
  // Icon pulses when active (handled by CSS)
}

function stopMicMonitoring(statusText) {
  micLoopActive = false;
  if (micTickTimer) {
    clearTimeout(micTickTimer);
    micTickTimer = null;
  }
  if (micStream) {
    micStream.getTracks().forEach((t) => t.stop());
    micStream = null;
  }
  if (micAudioCtx) {
    micAudioCtx.close().catch(() => {});
    micAudioCtx = null;
  }
  micInitialized = false;
  updateMicLevel(0);
  if (statusText) updateMicStatus(statusText);
}

function forceSubmitExamFromAutoStop(reasonText) {
  if (!examForm || examAutoSubmitted) return;
  examAutoSubmitted = true;
  const elapsedSeconds = Math.floor((Date.now() - examStartedAtMs) / 1000);
  if (timeTakenField) timeTakenField.value = String(elapsedSeconds);
  showBrowserWarning(reasonText || "Exam ended due to violations", "critical");
  sendEvent("exam_auto_end", reasonText || "Exam ended due to violations");
  examForm.submit();
}

function forceSubmitExamOnTimeout() {
  if (!examForm || examAutoSubmitted) return;
  examAutoSubmitted = true;
  const elapsedSeconds = Math.floor((Date.now() - examStartedAtMs) / 1000);
  if (timeTakenField) timeTakenField.value = String(elapsedSeconds);
  showBrowserWarning("Exam time is over. Submitting now.", "critical");
  sendEvent("time_over", "Exam time finished on client");
  examForm.submit();
}

function registerClientViolation(eventType, details, options = {}) {
  const level = options.level || "warning";
  setText(browserProcess, formatWarningType(eventType));
  showBrowserWarning(details, level);
  sendEvent(eventType, details);
}

function requestExamFullscreen() {
  const el = document.documentElement;
  if (!el || document.fullscreenElement || !el.requestFullscreen) return;
  el.requestFullscreen().catch(() => {
    // Fullscreen may require a user gesture; ignore failures.
  });
}

function isRestrictedShortcut(event) {
  const key = (event.key || "").toLowerCase();
  const ctrlOrMeta = event.ctrlKey || event.metaKey;

  if (event.metaKey) {
    return { type: "restricted_shortcut", details: "Windows/Command key pressed during exam" };
  }
  if (event.altKey && key === "tab") {
    return { type: "alt_tab_suspected", details: "Alt+Tab attempt detected during exam" };
  }
  if (ctrlOrMeta && (key === "tab" || key === "t" || key === "n" || key === "w")) {
    return { type: "restricted_shortcut", details: `Restricted browser shortcut detected: ${key.toUpperCase()}` };
  }
  if (ctrlOrMeta && event.shiftKey && (key === "i" || key === "j" || key === "c")) {
    return { type: "restricted_shortcut", details: "Developer tools shortcut detected during exam" };
  }
  if (ctrlOrMeta && key === "r") {
    return { type: "restricted_shortcut", details: "Page refresh shortcut detected during exam" };
  }
  if (key === "f11") {
    return { type: "restricted_shortcut", details: "Fullscreen toggle key detected during exam" };
  }
  if (ctrlOrMeta && key === "escape") {
    return { type: "restricted_shortcut", details: "Escape shortcut detected during exam" };
  }
  return null;
}

function updateWarningList(items) {
  if (!warningList) return;
  if (!items || items.length === 0) {
    warningList.innerHTML = '<li class="warn-item">No live warnings</li>';
    return;
  }

  warningList.innerHTML = items
    .map((item) => {
      const level = item.severity || "warning";
      const typeLabel = classifyWarningType(item.event_type, item.source);
      const time = item.created_at || "";
      return `<li class="warning-item ${level}">
        <span class="warning-meta">
          <span class="warning-pill type">${escapeHtml(typeLabel)}</span>
        </span>
        <span class="warning-main">
          <strong>${escapeHtml(formatWarningType(item.event_type))}</strong>
          ${time ? `<span class="warning-time">${escapeHtml(time)}</span>` : ""}
        </span>
        <span class="warning-detail">${escapeHtml(item.details || "Warning detected")}</span>
      </li>`;
    })
    .join("");
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function classifyWarningType(eventType, source) {
  if (AUDIO_EVENT_TYPES.has(eventType) || source === "audio") return "Audio";
  if (BROWSER_EVENT_TYPES.has(eventType) || source === "client") return "Browser";
  const faceEvents = new Set(["multiple_faces", "face_absent"]);
  const eyeEvents = new Set(["eyes_not_visible", "eye_looking_away"]);
  const headEvents = new Set(["looking_away", "head_movement"]);
  if (faceEvents.has(eventType)) return "Face";
  if (eyeEvents.has(eventType)) return "Eye";
  if (headEvents.has(eventType)) return "Head";
  if (VIDEO_EVENT_TYPES.has(eventType) || source === "video") return "Video";
  return "Video";
}

function formatWarningType(eventType) {
  const mapping = {
    multiple_faces: "Multiple faces detected",
    face_absent: "Face not clearly visible",
    eyes_not_visible: "Eyes not clearly visible",
    eye_looking_away: "Eye movement outside screen",
    looking_away: "Head movement / looking away",
    head_movement: "Head movement / looking away",
    voice_activity: "Voice activity detected",
    unwanted_talk: "Unwanted talking detected",
    background_noise: "Background noise detected",
    loud_noise: "Loud background noise",
    mic_access_denied: "Microphone access denied",
    mic_access_granted: "Microphone monitoring active",
    mic_disabled_by_admin: "Microphone disabled by admin",
    mic_enabled_by_admin: "Microphone enabled by admin",
    exam_auto_end: "Exam auto-stopped due to violations",
    tab_hidden: "Tab hidden",
    tab_switch_or_window_blur: "Another tab or window opened",
    tab_close_or_navigation: "Exam page close/navigation",
    restricted_shortcut: "Restricted shortcut",
    alt_tab_suspected: "Alt+Tab attempt",
    fullscreen_exit: "Fullscreen exit",
  };
  return mapping[eventType] || eventType || "Warning";
}

function setupQuestionPager() {
  if (!examForm) return;
  const questions = Array.from(examForm.querySelectorAll(".question[data-question-index]"));
  if (!questions.length) return;

  let currentPage = 0;
  const totalPages = Math.max(1, Math.ceil(questions.length / QUESTIONS_PER_PAGE));

  function pageQuestions(page) {
    const start = page * QUESTIONS_PER_PAGE;
    return questions.slice(start, start + QUESTIONS_PER_PAGE);
  }

  function questionAnswered(questionEl) {
    const controls = Array.from(
      questionEl.querySelectorAll("input, textarea, select")
    ).filter((control) => control.name && control.type !== "hidden");
    if (!controls.length) return true;

    const radioGroups = new Set(
      controls.filter((control) => control.type === "radio").map((control) => control.name)
    );
    for (const groupName of radioGroups) {
      if (!controls.some((control) => control.name === groupName && control.checked)) {
        return false;
      }
    }

    return controls
      .filter((control) => control.type !== "radio")
      .every((control) => !control.required || String(control.value || "").trim() !== "");
  }

  function validateCurrentPage() {
    const missing = pageQuestions(currentPage).find((questionEl) => !questionAnswered(questionEl));
    if (!missing) return true;
    const firstControl = missing.querySelector("input, textarea, select");
    missing.classList.add("question-needs-answer");
    window.setTimeout(() => missing.classList.remove("question-needs-answer"), 1300);
    if (firstControl) {
      firstControl.focus({ preventScroll: true });
    }
    missing.scrollIntoView({ behavior: "smooth", block: "center" });
    return false;
  }

  function renderPage() {
    const start = currentPage * QUESTIONS_PER_PAGE;
    const end = Math.min(start + QUESTIONS_PER_PAGE, questions.length);
    questions.forEach((questionEl, index) => {
      questionEl.hidden = index < start || index >= end;
    });
    setText(questionPagerStatus, `Questions ${start + 1}-${end} of ${questions.length}`);
    setText(questionPageCount, `Page ${currentPage + 1} of ${totalPages}`);
    if (prevQuestionPageBtn) prevQuestionPageBtn.disabled = currentPage === 0;
    if (nextQuestionPageBtn) nextQuestionPageBtn.hidden = currentPage >= totalPages - 1;
    if (examSubmitFooter) examSubmitFooter.hidden = currentPage < totalPages - 1;
  }

  if (prevQuestionPageBtn) {
    prevQuestionPageBtn.addEventListener("click", () => {
      currentPage = Math.max(0, currentPage - 1);
      renderPage();
    });
  }

  if (nextQuestionPageBtn) {
    nextQuestionPageBtn.addEventListener("click", () => {
      if (!validateCurrentPage()) return;
      currentPage = Math.min(totalPages - 1, currentPage + 1);
      renderPage();
    });
  }

  examForm.addEventListener("submit", (event) => {
    if (!validateCurrentPage()) {
      event.preventDefault();
    }
  });

  renderPage();
}

window.addEventListener("blur", () => {
  updateFocusLabel("Tab: inactive");
  setText(browserProcess, "Window opened/lost focus");
  const now = Date.now();
  if (now - lastTabViolationAt > 1200) {
    lastTabViolationAt = now;
    tabSwitchCount += 1;
    registerClientViolation(
      "tab_switch_or_window_blur",
      `Window lost focus (${tabSwitchCount})`,
      { level: "warning" }
    );
    if (autoStopEnabled && tabSwitchCount >= tabSwitchLimit) {
      forceSubmitExamFromAutoStop(
        `Exam stopped after ${tabSwitchCount} tab switches or focus losses`
      );
    }
  }
});

window.addEventListener("focus", () => {
  updateFocusLabel("Tab: active");
});

document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    updateFocusLabel("Tab: hidden");
    setText(browserProcess, "Tab hidden");
    const now = Date.now();
    if (now - lastTabViolationAt > 1200) {
      lastTabViolationAt = now;
      tabSwitchCount += 1;
      registerClientViolation(
        "tab_hidden",
        `Page is no longer visible (${tabSwitchCount})`,
        { level: "warning" }
      );
      if (autoStopEnabled && tabSwitchCount >= tabSwitchLimit) {
        forceSubmitExamFromAutoStop(
          `Exam stopped after ${tabSwitchCount} tab switches or hidden-tab events`
        );
      }
    }
  } else {
    updateFocusLabel("Tab: active");
    setText(browserProcess, "Active");
  }
});

window.addEventListener("beforeunload", () => {
  sendEvent("tab_close_or_navigation", "User is leaving the exam page");
});

window.addEventListener("contextmenu", (event) => {
  event.preventDefault();
});

document.addEventListener("keydown", (event) => {
  if (!examForm) return;
  const violation = isRestrictedShortcut(event);
  if (!violation) return;
  const now = Date.now();
  if (now - lastShortcutViolationAt < 1000) {
    event.preventDefault();
    return;
  }
  lastShortcutViolationAt = now;
  event.preventDefault();
  registerClientViolation(violation.type, violation.details, { level: "critical" });
});

document.addEventListener("fullscreenchange", () => {
  if (!examForm) return;
  if (!document.fullscreenElement) {
    registerClientViolation(
      "fullscreen_exit",
      "Fullscreen mode was exited during the exam",
      { level: "critical" }
    );
  }
});

async function initMicMonitoring() {
  if (!micAllowedByAdmin) {
    stopMicMonitoring("Mic: disabled by admin");
    return;
  }
  if (micInitialized || !navigator.mediaDevices?.getUserMedia) return;
  micInitialized = true;
  micLoopActive = true;
  updateMicStatus("Mic: requesting");
  try {
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
      video: false,
    });
    micStream = stream;

    const AudioCtx = window.AudioContext || window.webkitAudioContext;
    if (!AudioCtx) return;
    const audioCtx = new AudioCtx();
    micAudioCtx = audioCtx;
    const source = audioCtx.createMediaStreamSource(stream);
    const analyser = audioCtx.createAnalyser();
    analyser.fftSize = 2048;
    source.connect(analyser);

    const buf = new Float32Array(analyser.fftSize);
    const startedAt = performance.now();
    let speechStart = 0;
    let talkStart = 0;
    let noiseStart = 0;
    let lastNoiseEventAt = 0;
    let lastBackgroundEventAt = 0;
    let lastSpeechEventAt = 0;
    let lastTalkEventAt = 0;
    const cooldownMs = 5000;
    const speechDurationMs = 2500;
    const talkDurationMs = 3500;
    const backgroundDurationMs = 1800;
    const calibrationWindowMs = 4000;
    const calibrationDbSamples = [];
    let baselineDbfs = -55;
    let isCalibrated = false;
    const baselineNoiseMarginDb = 10;

    sendEvent("mic_access_granted", "Microphone monitoring started");
    updateMicStatus("Mic: active");
    setText(audioProcess, "Active");

    function rmsToDbfs(rms) {
      return 20 * Math.log10(Math.max(rms, 1e-8));
    }

    function tick() {
      analyser.getFloatTimeDomainData(buf);
      let sum = 0;
      for (let i = 0; i < buf.length; i += 1) sum += buf[i] * buf[i];
      const rms = Math.sqrt(sum / buf.length);
      const dbfs = rmsToDbfs(rms);
      const now = performance.now();
      updateMicLevel(rms);

      const loudNoise = rms >= 0.085;
      const speechLike = rms >= 0.02 && rms <= 0.085;
      const talkLike = rms >= 0.028;
      const isCalibrationPhase = now - startedAt <= calibrationWindowMs;

      if (isCalibrationPhase) {
        calibrationDbSamples.push(dbfs);
      } else if (!isCalibrated && calibrationDbSamples.length > 0) {
        const sorted = calibrationDbSamples.slice().sort((a, b) => a - b);
        baselineDbfs = sorted[Math.floor(sorted.length / 2)];
        isCalibrated = true;
      }

      const backgroundThresholdDbfs = baselineDbfs + baselineNoiseMarginDb;
      const backgroundNoise = isCalibrated && dbfs >= backgroundThresholdDbfs && rms >= 0.012;

      if (loudNoise && now - lastNoiseEventAt > cooldownMs) {
        lastNoiseEventAt = now;
        setText(audioProcess, "Loud noise");
        sendEvent(
          "loud_noise",
          `Loud background noise from student mic (RMS=${rms.toFixed(3)}, dBFS=${dbfs.toFixed(1)})`
        );
      }

      if (backgroundNoise) {
        if (!noiseStart) noiseStart = now;
        const noiseFor = now - noiseStart;
        if (
          noiseFor >= backgroundDurationMs &&
          now - lastBackgroundEventAt > cooldownMs
        ) {
          lastBackgroundEventAt = now;
          setText(audioProcess, "Background noise");
          sendEvent(
            "background_noise",
            `Background noise above room baseline for ${(noiseFor / 1000).toFixed(
              1
            )}s (current=${dbfs.toFixed(1)} dBFS, baseline=${baselineDbfs.toFixed(
              1
            )} dBFS)`
          );
        }
      } else {
        noiseStart = 0;
        // Slowly adapt baseline during calm periods.
        if (isCalibrated && !speechLike && !talkLike && dbfs < backgroundThresholdDbfs - 3) {
          baselineDbfs = baselineDbfs * 0.98 + dbfs * 0.02;
        }
      }

      if (speechLike) {
        if (!speechStart) speechStart = now;
        const speechFor = now - speechStart;
        if (speechFor >= speechDurationMs && now - lastSpeechEventAt > cooldownMs) {
          lastSpeechEventAt = now;
          setText(audioProcess, "Voice activity");
          sendEvent(
            "voice_activity",
            `Student voice activity detected for ${(speechFor / 1000).toFixed(1)}s (RMS=${rms.toFixed(3)}, dBFS=${dbfs.toFixed(1)})`
          );
        }
      } else {
        speechStart = 0;
      }

      if (talkLike) {
        if (!talkStart) talkStart = now;
        const talkFor = now - talkStart;
        if (talkFor >= talkDurationMs && now - lastTalkEventAt > cooldownMs) {
          lastTalkEventAt = now;
          setText(audioProcess, "Talking detected");
          sendEvent(
            "unwanted_talk",
            `Unwanted talking detected for ${(talkFor / 1000).toFixed(1)}s (RMS=${rms.toFixed(3)}, dBFS=${dbfs.toFixed(1)})`
          );
        }
      } else {
        talkStart = 0;
      }

      // Keep monitoring loop responsive but light.
      if (micLoopActive && performance.now() - startedAt < 24 * 60 * 60 * 1000) {
        micTickTimer = window.setTimeout(tick, 200);
      }
    }

    tick();
  } catch (err) {
    sendEvent("mic_access_denied", "Microphone access denied or unavailable");
    stopMicMonitoring("Mic: denied");
  }
}

async function syncMonitoringConfigFromAdmin() {
  try {
    const res = await fetch("/api/monitoring_config");
    if (!res.ok) return;
    const cfg = await res.json();
    const enabled = !!cfg.student_mic_enabled;
    if (enabled !== micAllowedByAdmin) {
      micAllowedByAdmin = enabled;
      if (!enabled) {
        stopMicMonitoring("Mic: disabled by admin");
        sendEvent("mic_disabled_by_admin", "Admin turned off microphone monitoring");
      } else {
        updateMicStatus("Mic: enabled by admin");
        sendEvent("mic_enabled_by_admin", "Admin turned on microphone monitoring");
        initMicMonitoring();
      }
    }

    const nextAutoStopEnabled = !!cfg.auto_stop_enabled;
    if (nextAutoStopEnabled && !autoStopEnabled) {
      autoStopArmedAtMs = Date.now();
      tabSwitchCount = 0;
    }
    autoStopEnabled = nextAutoStopEnabled;
    autoStopWarningLimit = Math.max(1, Number(cfg.auto_stop_warning_limit || 5));
    autoStopCriticalLimit = Math.max(1, Number(cfg.auto_stop_critical_limit || 2));
    tabSwitchLimit = Math.max(1, Number(cfg.tab_switch_limit || 1));
    maxWarningsBannerCount = autoStopWarningLimit;
  } catch (e) {
    // Keep current state if config polling fails.
  }
}

async function refreshLiveWarnings() {
  if (!warningList) return;
  try {
    const [liveRes, eventRes] = await Promise.all([
      fetch("/api/live_video_warnings"),
      fetch("/api/student_events"),
    ]);
    const liveRows = liveRes.ok ? await liveRes.json() : [];
    const rows = eventRes.ok ? await eventRes.json() : [];
    const monitoredEvents = rows.filter(
      (row) =>
        row &&
        ["video", "audio", "client"].includes(row.source) &&
        (VIDEO_EVENT_TYPES.has(row.event_type) ||
          AUDIO_EVENT_TYPES.has(row.event_type) ||
          BROWSER_EVENT_TYPES.has(row.event_type) ||
          AUTO_STOP_EVENTS.has(row.event_type))
    );
    const warningEvents = monitoredEvents.filter(isWarningEvent).slice(0, 50);
    if (autoStopEnabled && warningEvents.length) {
      let warningCountSinceArmed = 0;
      let criticalCountSinceArmed = 0;
      for (const row of warningEvents) {
        if (!row || !row.event_type || !AUTO_STOP_EVENTS.has(row.event_type)) continue;
        if (row.created_at) {
          const createdMs = Date.parse(row.created_at + "Z");
          if (!Number.isNaN(createdMs) && createdMs < autoStopArmedAtMs) continue;
        }
        warningCountSinceArmed += 1;
        if ((row.severity || "warning") === "critical") criticalCountSinceArmed += 1;
      }
      if (
        warningCountSinceArmed >= autoStopWarningLimit ||
        criticalCountSinceArmed >= autoStopCriticalLimit
      ) {
        forceSubmitExamFromAutoStop(
          `Auto-stopped after ${warningCountSinceArmed} warnings and ${criticalCountSinceArmed} critical warnings`
        );
      }
    }
    const recentEvents = sortWarningsNewestFirst(recentWarningItems(warningEvents));
    const combined = [];
    for (const row of liveRows) {
      if (!isWarningEvent(row)) continue;
      combined.push(row);
    }
    for (const row of recentEvents) {
      if (!isWarningEvent(row)) continue;
      combined.push(row);
    }
    updateWarningList(combined.slice(0, MAX_VISIBLE_WARNINGS));
    updateMonitoringSummary([...liveRows, ...warningEvents]);
  } catch (e) {
    // Keep existing UI state on polling failures.
  }
}

// Simple client-side exam timer
if (examForm && timerLabel && timeTakenField) {
  const totalSeconds = parseInt(
    examForm.getAttribute("data-duration-seconds") || "0",
    10
  );
  if (totalSeconds > 0) {
    const startedAt = Date.now();

    function updateTimer() {
      const elapsedSeconds = Math.floor((Date.now() - startedAt) / 1000);
      const remaining = Math.max(totalSeconds - elapsedSeconds, 0);
      const m = Math.floor(remaining / 60);
      const s = remaining % 60;
      timerLabel.textContent = `Time left: ${m.toString().padStart(2, "0")}:${s
        .toString()
        .padStart(2, "0")}`;
      timeTakenField.value = String(elapsedSeconds);

      if (remaining <= 0) {
        timerLabel.textContent = "Time is over";
        forceSubmitExamOnTimeout();
      }
    }

    updateTimer();
    setInterval(updateTimer, 1000);
  } else {
    timerLabel.textContent = "No time limit";
  }

  setupQuestionPager();
  refreshLiveWarnings();
  setInterval(refreshLiveWarnings, 1000);
  (async () => {
    await syncMonitoringConfigFromAdmin();
    requestExamFullscreen();
    setInterval(syncMonitoringConfigFromAdmin, 3000);
    if (micAllowedByAdmin) {
      initMicMonitoring();
    } else {
      stopMicMonitoring("Mic: disabled by admin");
    }
  })();

  ["click", "keydown"].forEach((eventName) => {
    window.addEventListener(
      eventName,
      () => {
        requestExamFullscreen();
      },
      { passive: true }
    );
  });
}
