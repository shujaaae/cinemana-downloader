/* ===== Aurora front-end logic =======================================
 * Talks to Python via window.pywebview.api.<method>(...) and receives a
 * single stream of window.onAppEvent([...]) batches pushed from the host.
 * Engine behaviour lives in Python; this file is presentation + wiring only.
 * ==================================================================== */

"use strict";

const DEFAULT_LANG = "ar";

/* New, Aurora-only chrome strings. Everything the engine already knows about
 * comes from the Python i18n table (merged in at boot), so this stays small and
 * we never duplicate / fork an engine string. */
const EXTRA_STRINGS = {
  nav_download:      { ar: "تنزيل", en: "Download" },
  nav_library:       { ar: "المكتبة", en: "Library" },
  nav_settings:      { ar: "الإعدادات", en: "Settings" },
  btn_refresh:       { ar: "تحديث", en: "Refresh" },
  library_empty:     { ar: "لا توجد تنزيلات بعد.", en: "No downloads yet." },
  lib_done_count:    { ar: "{done} من {total} مكتمل", en: "{done} of {total} done" },
  lib_summary:       { ar: "{series} عنوان · {files} ملف", en: "{series} titles · {files} files" },
  btn_open_file:     { ar: "فتح", en: "Open" },
  btn_open_folder:   { ar: "المجلد", en: "Folder" },
  set_language:      { ar: "اللغة", en: "Language" },
  set_language_hint: { ar: "لغة الواجهة واتجاهها.", en: "Interface language and direction." },
  set_save_folder:   { ar: "مجلد الحفظ الافتراضي", en: "Default save folder" },
  set_quality:       { ar: "الجودة الافتراضية", en: "Default quality" },
  set_quality_hint:  { ar: "تُطبَّق على العناوين المجلوبة حديثًا.", en: "Applied to newly fetched titles." },
  set_concurrency:   { ar: "التزامن الافتراضي", en: "Default concurrency" },
  set_concurrency_hint:{ ar: "عدد الملفات المنزَّلة معًا.", en: "Files downloaded at once." },
  set_segments:      { ar: "المقاطع الافتراضية", en: "Default segments" },
  set_segments_hint: { ar: "عدد الأجزاء لكل ملف (مثل IDM).", en: "Parts per file (IDM-style)." },
  saving_to:         { ar: "الحفظ في", en: "Saving to" },
  disk_free:         { ar: "{free} متاح من {total}", en: "{free} free of {total}" },
  ph_paste_link:     { ar: "ألصق رابط مسلسل أو فيلم من سينمانا…", en: "Paste a Cinemana series or movie link…" },
  btn_fetch_short:   { ar: "جلب", en: "Fetch" },
  opt_quality:       { ar: "الجودة", en: "Quality" },
  opt_subtitles:     { ar: "الترجمة", en: "Subtitles" },
  opt_subs_all:      { ar: "كل اللغات", en: "All AR·EN" },
  opt_subs_ar:       { ar: "العربية فقط", en: "Arabic only" },
  opt_subs_en:       { ar: "الإنجليزية فقط", en: "English only" },
  opt_subs_both:     { ar: "العربية والإنجليزية", en: "Both (AR·EN)" },
  opt_concurrency:   { ar: "التزامن", en: "Concurrency" },
  opt_segments:      { ar: "المقاطع", en: "Segments" },
  episodes_title:    { ar: "الحلقات", en: "Episodes" },
  ep_complete:       { ar: "{done} من {total} مكتملة", en: "{done} of {total} complete" },
  btn_rescan:        { ar: "إعادة فحص", en: "Rescan" },
  scan_summary:      { ar: "{done} مُنزَّلة · {missing} غير مُنزَّلة", en: "{done} downloaded · {missing} not downloaded" },
  btn_pause_short:   { ar: "إيقاف", en: "Pause" },
  overall_progress:  { ar: "التقدّم الكلي · {pct}٪", en: "Overall progress · {pct}%" },
  overall_rate_left: { ar: "↓ {speed} · {eta} متبقٍّ", en: "↓ {speed} · {eta} left" },
  seasons_episodes:  { ar: "{s} مواسم · {e} حلقة", en: "{s} Seasons · {e} Episodes" },
  one_season_episodes:{ ar: "موسم واحد · {e} حلقة", en: "1 Season · {e} Episodes" },
  movie_chip:        { ar: "فيلم", en: "Movie" },
  conc_files:        { ar: "{n} ملفات", en: "{n} files" },
  seg_per_file:      { ar: "{n} لكل ملف", en: "{n} / file" },
  nav_log:           { ar: "السجل", en: "Log" },
  btn_clear_log:     { ar: "مسح", en: "Clear" },
  log_empty:         { ar: "لا يوجد نشاط بعد.", en: "No activity yet." },
  log_count:         { ar: "{n} حدث", en: "{n} entries" },
  log_ui_fetch:      { ar: "طلب جلب: {url}", en: "Fetch requested: {url}" },
  log_ui_start:      { ar: "بدء التنزيل — {n} حلقة بجودة {h}", en: "Download started — {n} episode(s) at {h}" },
  log_ui_pause_all:  { ar: "إيقاف الكل مؤقتًا", en: "Pause all" },
  log_ui_resume_all: { ar: "استئناف الكل", en: "Resume all" },
  log_ui_stop:       { ar: "إيقاف التنزيل", en: "Stop download" },
  log_ui_ep_pause:   { ar: "إيقاف مؤقت: {label}", en: "Pause: {label}" },
  log_ui_ep_resume:  { ar: "استئناف: {label}", en: "Resume: {label}" },
  log_ui_ep_cancel:  { ar: "إلغاء: {label}", en: "Cancel: {label}" },
  log_ui_dest:       { ar: "تغيير مجلد الحفظ: {dest}", en: "Save folder changed: {dest}" },
  log_ui_lang:       { ar: "تغيير اللغة إلى {lang}", en: "Language changed to {lang}" },
  log_ui_prefs:      { ar: "تحديث الإعدادات الافتراضية", en: "Default settings updated" },
};

/* ----- runtime state ------------------------------------------------ */
let STRINGS = {};
let LANG = DEFAULT_LANG;

const state = {
  plan: null,
  hero: null,
  heights: [],
  defaults: { concurrency: 3, segments: 4, max_connections: 32, max_concurrency: 6, max_segments: 32 },
  prefs: { quality_height: null, concurrency: 3, segments: 4 },  // persisted Settings-tab defaults
  dest: "",
  running: false,
  fetching: false,
  view: "download",   // active nav view: download | library | settings
  libData: null,      // last get_library() payload (for relabel on lang switch)
};

// Common quality ladder for the Settings "default quality" picker (no plan needed).
const COMMON_HEIGHTS = [2160, 1080, 720, 480, 360, 240];

const allNbs = [];                 // every episode nb, in display order
const checked = new Map();         // nb -> bool
const collapsed = new Set();       // season number collapsed
const epEls = new Map();           // nb -> { row, chk, mid, meta, rate, pct, pill, segbar, segBlocks, season }
const seasonEls = new Map();       // season -> { group, chk, nameEl, countEl, body, chevron, kids:[nb] }
const epStatus = new Map();        // nb -> status string
const epProgress = new Map();      // nb -> fraction 0..1
const epSize = new Map();          // nb -> { done, total }

const CHK = { on: "☑", off: "☐", partial: "◪" };

/* ----- DOM cache ---------------------------------------------------- */
let dom = {};
function cacheDom() {
  const id = (x) => document.getElementById(x);
  dom = {
    app: id("app"),
    langToggle: id("lang-toggle"),
    winMin: id("win-min"), winMax: id("win-max"), winClose: id("win-close"),
    destPath: id("dest-path"), diskFill: id("disk-fill"), diskMeta: id("disk-meta"),
    browseBtn: id("browse-btn"),
    url: id("url"), pasteBtn: id("paste-btn"), fetchBtn: id("fetch-btn"),
    hero: id("hero"),
    poster: id("hero-poster"), posterImg: id("hero-poster-img"),
    heroTitle: id("hero-title"), heroImdb: id("hero-imdb"),
    heroMeta: id("hero-meta"), heroChip: id("hero-chip"), heroSynopsis: id("hero-synopsis"),
    optQuality: id("opt-quality"), optSubs: id("opt-subs"),
    optConc: id("opt-concurrency"), optSeg: id("opt-segments"),
    connWarn: id("conn-warn"),
    epToolbar: id("ep-toolbar"), epComplete: id("ep-complete"), scanSummary: id("scan-summary"),
    rescanBtn: id("rescan-btn"),
    selAll: id("sel-all"), selNone: id("sel-none"), selInvert: id("sel-invert"),
    epList: id("ep-list"), emptyState: id("empty-state"),
    overallLabel: id("overall-label"), overallRate: id("overall-rate"), overallFill: id("overall-fill"),
    pauseBtn: id("pause-btn"), playBtn: id("play-btn"), stopBtn: id("stop-btn"),
    ctxMenu: id("ctx-menu"), toast: id("toast"),
    // views / router
    nav: document.querySelector(".nav"),
    viewDownload: id("view-download"), viewLibrary: id("view-library"), viewSettings: id("view-settings"),
    // library
    libList: id("lib-list"), libSummary: id("lib-summary"), libRefresh: id("lib-refresh"),
    // settings
    setLangToggle: id("set-lang-toggle"), setDestPath: id("set-dest-path"), setBrowseBtn: id("set-browse-btn"),
    setQuality: id("set-quality"), setConc: id("set-concurrency"), setSeg: id("set-segments"),
    // log
    viewLog: id("view-log"), logList: id("log-list"), logCount: id("log-count"),
    logClear: id("log-clear"), logEmpty: id("log-empty"),
  };
}

/* ===== i18n ========================================================= */
function fmt(tpl, params) {
  return tpl.replace(/\{(\w+)(?::0(\d+)d)?\}/g, (m, key, pad) => {
    if (!params || !(key in params)) return m;
    const v = params[key];
    return pad ? String(v).padStart(parseInt(pad, 10), "0") : String(v);
  });
}
function t(key, params) {
  const e = STRINGS[key];
  if (!e) return key;
  const tpl = e[LANG] || e[DEFAULT_LANG] || key;
  return params ? fmt(tpl, params) : tpl;
}

/* ===== formatting helpers (mirror cinemana/rate.py) ================= */
function humanSize(n) {
  if (!n) return "";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let f = Number(n);
  for (const u of units) {
    if (f < 1024 || u === "TB") return (u === "B" ? f.toFixed(0) : f.toFixed(1)) + " " + u;
    f /= 1024;
  }
  return String(n);
}
function humanSpeed(bps) {
  if (!bps || bps <= 0) return "";
  const units = ["B/s", "KB/s", "MB/s", "GB/s"];
  let f = Number(bps);
  for (const u of units) {
    if (f < 1024 || u === "GB/s") return (u === "B/s" ? f.toFixed(0) : f.toFixed(1)) + " " + u;
    f /= 1024;
  }
  return "";
}
function formatEta(sec) {
  if (sec === null || sec === undefined || sec !== sec || sec <= 0) return "";
  let s = Math.floor(sec);
  if (s >= 3600) {
    const h = Math.floor(s / 3600); s %= 3600;
    return `${h}:${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
  }
  return `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
}

/* ===== pywebview bridge ============================================= */
function py(method, ...args) {
  try {
    if (window.pywebview && window.pywebview.api && window.pywebview.api[method]) {
      return window.pywebview.api[method](...args);
    }
  } catch (e) { console.error("py(" + method + ")", e); }
  return Promise.resolve(null);
}

let saveTimer = null;
function scheduleSave() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => py("save_state", currentForm()), 400);
}
function currentForm() {
  return {
    url: dom.url.value,
    dest: state.dest,
    quality_height: selectedHeight(),
    concurrency: parseInt(dom.optConc.value, 10) || state.defaults.concurrency,
    segments: parseInt(dom.optSeg.value, 10) || state.defaults.segments,
    subtitle_langs: dom.optSubs.value || "ar",
    selected_nbs: selectedNbs(),
  };
}
function selectedHeight() {
  const v = parseInt(dom.optQuality.value, 10);
  return v || (state.plan ? state.plan.default_height : 1080);
}
function selectedNbs() {
  return allNbs.filter((nb) => checked.get(nb));
}

/* ===== boot ========================================================= */
function applyBootstrap(data) {
  STRINGS = Object.assign({}, data.strings || {}, EXTRA_STRINGS);
  state.defaults = data.defaults || state.defaults;

  // Persisted Settings-tab defaults: fold them into the live defaults so a
  // fresh fetch (no session override) uses the user's preferred values.
  const prefs = data.prefs || {};
  state.prefs = {
    quality_height: prefs.quality_height || null,
    concurrency: prefs.concurrency || state.defaults.concurrency,
    segments: prefs.segments || state.defaults.segments,
  };
  state.defaults.concurrency = state.prefs.concurrency;
  state.defaults.segments = state.prefs.segments;

  setLang(data.lang || DEFAULT_LANG, false);

  const s = data.session || {};
  state.dest = s.dest || "";
  dom.url.value = s.url || "";
  updateDisk(data.disk);
  setDestText(state.dest);
  buildSettingsControls();   // settings form reflects prefs + dest

  if (data.plan) {
    renderPlan(data.plan, new Set(s.selected_nbs || []), {
      quality_height: s.quality_height, concurrency: s.concurrency, segments: s.segments,
    });
    if (data.hero) renderHero(data.hero);
    applyScan(data.statuses || {}, data.scan_summary);
    toast(t("log_session_restored"), "info", 2600);
  } else {
    setOptionDefaults(s);
  }
  updateButtons();
}

/* ===== language / RTL =============================================== */
function setLang(lang, persist) {
  LANG = (lang === "en") ? "en" : "ar";
  const dir = LANG === "ar" ? "rtl" : "ltr";
  document.documentElement.lang = LANG;
  document.documentElement.dir = dir;
  dom.app.dir = dir;
  // Both the titlebar toggle and the Settings-page toggle share .lang-opt.
  document.querySelectorAll(".lang-toggle .lang-opt").forEach((b) => {
    b.classList.toggle("active", b.dataset.lang === LANG);
  });
  applyI18n();
  if (persist) {
    py("set_language", LANG);
    appendLog(t("log_ui_lang", { lang: LANG === "ar" ? "العربية" : "English" }), "action");
  }
}

function applyI18n() {
  document.querySelectorAll("[data-i18n]").forEach((el) => { el.textContent = t(el.dataset.i18n); });
  document.querySelectorAll("[data-i18n-ph]").forEach((el) => { el.placeholder = t(el.dataset.i18nPh); });
  // dynamic, language-dependent bits
  if (dom.optSubs) fillSubsSelect(dom.optSubs.value || "ar");
  relabelTree();
  refreshPills();
  renderScanSummary(lastScanSummary);
  if (state.plan) renderHeroChip();
  if (state.hero) renderHeroMeta();
  recomputeOverall();
  rebuildSettingsLabels();
  if (state.view === "library" && state.libData) renderLibrary(state.libData);
}

/* ===== disk / dest ================================================== */
function setDestText(dest) {
  dom.destPath.textContent = dest || "—";
  dom.destPath.title = dest || "";
}
function updateDisk(disk) {
  if (!disk) return;
  dom.diskFill.style.width = (disk.used_pct || 0) + "%";
  dom.diskMeta.textContent = disk.free_label
    ? t("disk_free", { free: disk.free_label, total: disk.total_label }) : "";
}

/* ===== options (quality / concurrency / segments) =================== */
function setOptionDefaults(sess) {
  buildConcSeg(sess);
}
function buildConcSeg(sess) {
  const conc = (sess && sess.concurrency) || state.defaults.concurrency;
  const seg = (sess && sess.segments) || state.defaults.segments;
  fillSelect(dom.optConc, range(1, state.defaults.max_concurrency || 6),
    (n) => t("conc_files", { n }), conc);
  fillSelect(dom.optSeg, range(1, state.defaults.max_segments || 32),
    (n) => t("seg_per_file", { n }), seg);
  fillSubsSelect(sess && sess.subtitle_langs);
}
const SUBS_OPTS = ["ar", "en", "both"];
function fillSubsSelect(current) {
  fillSelect(dom.optSubs, SUBS_OPTS, (v) => t("opt_subs_" + v), current || "ar");
}
function fillSelect(sel, values, labelFn, current) {
  sel.innerHTML = "";
  for (const v of values) {
    const o = document.createElement("option");
    o.value = String(v);
    o.textContent = labelFn(v);
    if (String(v) === String(current)) o.selected = true;
    sel.appendChild(o);
  }
}
function range(a, b) { const out = []; for (let i = a; i <= b; i++) out.push(i); return out; }

function updateConnWarn() {
  const n = parseInt(dom.optConc.value, 10) || 1;
  const m = parseInt(dom.optSeg.value, 10) || 1;
  const max = state.defaults.max_connections || 32;
  if (n * m > max) {
    dom.connWarn.textContent = t("conn_cap_warn", { max }).trim();
    dom.connWarn.classList.remove("hidden");
  } else {
    dom.connWarn.classList.add("hidden");
  }
}

/* ===== hero ========================================================= */
function renderHero(hero) {
  state.hero = hero || null;
  if (!hero) return;
  if (hero.poster) {
    dom.posterImg.src = hero.poster;
    dom.posterImg.onload = () => dom.poster.classList.add("has-img");
    dom.posterImg.onerror = () => dom.poster.classList.remove("has-img");
  } else {
    dom.poster.classList.remove("has-img");
  }
  if (hero.imdb) {
    dom.heroImdb.textContent = " " + hero.imdb;
    dom.heroImdb.classList.remove("hidden");
  } else {
    dom.heroImdb.classList.add("hidden");
  }
  dom.heroSynopsis.textContent = hero.synopsis || "";
  dom.heroSynopsis.classList.toggle("hidden", !hero.synopsis);
  renderHeroMeta();
  renderHeroChip();
}
function renderHeroMeta() {
  const h = state.hero;
  if (!h) { dom.heroMeta.textContent = ""; return; }
  const parts = [];
  if (h.year) parts.push(h.year);
  if (h.genres && h.genres.length) parts.push(h.genres.join(" · "));
  dom.heroMeta.textContent = parts.join("  ·  ");
}
function renderHeroChip() {
  const p = state.plan;
  if (!p) return;
  let txt;
  if (p.is_movie) txt = t("movie_chip");
  else if (p.n_seasons <= 1) txt = t("one_season_episodes", { e: p.n_episodes });
  else txt = t("seasons_episodes", { s: p.n_seasons, e: p.n_episodes });
  dom.heroChip.textContent = txt;
}

/* ===== plan / episode list ========================================= */
function renderPlan(plan, preselect, sessOpts) {
  state.plan = plan;
  state.heights = plan.heights || [];
  allNbs.length = 0;
  checked.clear(); collapsed.clear(); seasonEls.clear(); epEls.clear();
  epStatus.clear(); epProgress.clear(); epSize.clear();

  // hero shell (title + chip); rich metadata fills in via renderHero later
  dom.heroTitle.textContent = plan.title || "";
  dom.heroImdb.classList.add("hidden");
  dom.heroMeta.textContent = "";
  dom.heroSynopsis.textContent = "";
  dom.poster.classList.remove("has-img");
  state.hero = null;
  renderHeroChip();
  dom.hero.classList.remove("hidden");
  dom.epToolbar.classList.remove("hidden");
  renderScanSummary(null);   // clear stale counts until the scan event lands

  // quality options — precedence: last-session value, then the saved Settings
  // default (when that height exists for this title), then the plan default.
  const prefH = state.prefs && state.prefs.quality_height
    && state.heights.some((h) => h.value === state.prefs.quality_height)
    ? state.prefs.quality_height : null;
  fillSelect(dom.optQuality, state.heights.map((h) => h.value),
    (v) => (state.heights.find((h) => h.value === v) || {}).label || (v + "p"),
    (sessOpts && sessOpts.quality_height) || prefH || plan.default_height);
  buildConcSeg(sessOpts || {});
  updateConnWarn();

  // episode rows grouped by season (first-seen order preserved)
  dom.epList.innerHTML = "";
  const groups = new Map();   // season -> group element record
  const selectAll = !preselect;

  for (const ep of plan.episodes) {
    allNbs.push(ep.nb);
    const isChecked = selectAll ? true : preselect.has(ep.nb);
    checked.set(ep.nb, isChecked);
    epStatus.set(ep.nb, "pending");

    let body;
    if (ep.is_movie) {
      body = ensureLooseBody();
    } else {
      if (!groups.has(ep.season)) groups.set(ep.season, makeSeasonGroup(ep.season));
      body = groups.get(ep.season).body;
    }
    const row = makeEpRow(ep);
    body.appendChild(row);
    if (!ep.is_movie) seasonEls.get(ep.season).kids.push(ep.nb);
  }
  for (const season of seasonEls.keys()) updateSeasonGlyph(season);
  recomputeOverall();
  scheduleSave();
}

let looseBody = null;
function ensureLooseBody() {
  if (looseBody && looseBody.isConnected) return looseBody;
  const g = document.createElement("div");
  g.className = "season-group";
  looseBody = document.createElement("div");
  looseBody.className = "season-body";
  g.appendChild(looseBody);
  dom.epList.appendChild(g);
  return looseBody;
}

function makeSeasonGroup(season) {
  const group = document.createElement("div");
  group.className = "season-group";
  group.dataset.season = season;

  const header = document.createElement("div");
  header.className = "season-header";

  const chk = document.createElement("span");
  chk.className = "chk";
  chk.textContent = CHK.off;

  const chevron = document.createElement("span");
  chevron.className = "season-chevron";
  chevron.innerHTML = '<svg viewBox="0 0 14 14"><path d="M3.5 5 7 8.5 10.5 5" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>';

  const name = document.createElement("span");
  name.className = "season-name";
  name.textContent = t("season_label", { n: season });

  const count = document.createElement("span");
  count.className = "season-count";

  header.append(chk, chevron, name, count);

  const body = document.createElement("div");
  body.className = "season-body";

  group.append(header, body);
  dom.epList.appendChild(group);

  header.addEventListener("click", (e) => {
    if (e.target.closest(".season-chevron")) { toggleCollapse(season); return; }
    toggleSeason(season);
  });

  const rec = { group, chk, nameEl: name, countEl: count, body, chevron, kids: [] };
  seasonEls.set(season, rec);
  return rec;
}

function makeEpRow(ep) {
  const row = document.createElement("div");
  row.className = "ep-row";
  row.dataset.nb = ep.nb;

  const chk = document.createElement("span");
  chk.className = "chk";

  const code = document.createElement("span");
  code.className = "ep-code";
  code.textContent = ep.is_movie ? t("movie_label") : ep.label;

  const title = document.createElement("span");
  title.className = "ep-title";
  title.textContent = ep.title || "";

  const mid = document.createElement("div");
  mid.className = "ep-mid";
  const meta = document.createElement("div");
  meta.className = "ep-meta";
  mid.appendChild(meta);

  const trail = document.createElement("div");
  trail.className = "ep-trail";
  const rate = document.createElement("span"); rate.className = "ep-rate";
  const pct = document.createElement("span"); pct.className = "ep-pct";
  const pill = document.createElement("span"); pill.className = "pill pending"; pill.textContent = t("status_pending");
  trail.append(rate, pct, pill);

  row.append(chk, code, title, mid, trail);

  const refs = { row, chk, code, title, mid, meta, rate, pct, pill, segbar: null, segBlocks: null, season: ep.is_movie ? null : ep.season, isMovie: ep.is_movie };
  epEls.set(ep.nb, refs);
  renderChk(ep.nb);

  row.addEventListener("click", () => toggleEpisode(ep.nb));
  row.addEventListener("contextmenu", (e) => { e.preventDefault(); openCtx(e, ep.nb); });
  return row;
}

/* ----- selection / tri-state --------------------------------------- */
function renderChk(nb) {
  const refs = epEls.get(nb); if (!refs) return;
  const on = checked.get(nb);
  refs.chk.textContent = on ? CHK.on : CHK.off;
  refs.chk.classList.toggle("on", !!on);
}
function toggleEpisode(nb) {
  checked.set(nb, !checked.get(nb));
  renderChk(nb);
  const refs = epEls.get(nb);
  if (refs && refs.season != null) updateSeasonGlyph(refs.season);
  recomputeOverall();
  scheduleSave();
}
function toggleSeason(season) {
  const rec = seasonEls.get(season); if (!rec) return;
  const target = !rec.kids.every((nb) => checked.get(nb));
  for (const nb of rec.kids) { checked.set(nb, target); renderChk(nb); }
  updateSeasonGlyph(season);
  recomputeOverall();
  scheduleSave();
}
function toggleCollapse(season) {
  const rec = seasonEls.get(season); if (!rec) return;
  if (collapsed.has(season)) { collapsed.delete(season); rec.group.classList.remove("collapsed"); }
  else { collapsed.add(season); rec.group.classList.add("collapsed"); }
}
function updateSeasonGlyph(season) {
  const rec = seasonEls.get(season); if (!rec) return;
  const on = rec.kids.filter((nb) => checked.get(nb)).length;
  let glyph = CHK.off, cls = "";
  if (on === rec.kids.length && on > 0) { glyph = CHK.on; cls = "on"; }
  else if (on > 0) { glyph = CHK.partial; cls = "partial"; }
  rec.chk.textContent = glyph;
  rec.chk.classList.toggle("on", cls === "on");
  rec.chk.classList.toggle("partial", cls === "partial");
  rec.countEl.textContent = t("kind_episodes", { n: rec.kids.length });
}
function selectAllEpisodes(val) {
  for (const nb of allNbs) { checked.set(nb, val); renderChk(nb); }
  for (const s of seasonEls.keys()) updateSeasonGlyph(s);
  recomputeOverall(); scheduleSave();
}
function invertSelection() {
  for (const nb of allNbs) { checked.set(nb, !checked.get(nb)); renderChk(nb); }
  for (const s of seasonEls.keys()) updateSeasonGlyph(s);
  recomputeOverall(); scheduleSave();
}

/* ----- relabel on language switch ---------------------------------- */
function relabelTree() {
  for (const [season, rec] of seasonEls) {
    rec.nameEl.textContent = t("season_label", { n: season });
    rec.countEl.textContent = t("kind_episodes", { n: rec.kids.length });
  }
  for (const [nb, refs] of epEls) {
    if (refs.isMovie) refs.code.textContent = t("movie_label");
  }
}
function refreshPills() {
  for (const [nb, refs] of epEls) {
    const st = epStatus.get(nb) || "pending";
    refs.pill.textContent = t("status_" + st);
  }
}

/* ===== live status / progress ====================================== */
function setStatus(nb, status, extra) {
  epStatus.set(nb, status);
  const refs = epEls.get(nb); if (!refs) return;
  refs.pill.className = "pill " + status;
  refs.pill.textContent = t("status_" + status);
  if (status === "done") {
    if (extra && extra.size) epSize.set(nb, { done: extra.size, total: extra.size });
    epProgress.set(nb, 1);
    refs.pct.textContent = "100%";
    refs.rate.textContent = "";
    removeSeg(nb);
  } else if (status === "paused") {
    refs.rate.textContent = "";
    removeSeg(nb);
  } else if (status === "error") {
    refs.rate.textContent = "";
    refs.pct.textContent = "—";
    removeSeg(nb);
  } else if (status === "pending") {
    refs.rate.textContent = ""; refs.pct.textContent = "";
    removeSeg(nb);
  }
  recomputeOverall();
}
function setProgress(nb, done, total) {
  const refs = epEls.get(nb); if (!refs) return;
  epSize.set(nb, { done, total });
  if (total) {
    refs.pct.textContent = Math.min(100, Math.floor(done * 100 / total)) + "%";
    epProgress.set(nb, Math.min(1, done / total));
    // mirror the file fraction into a lone indeterminate block
    if (refs.segBlocks && refs.segBlocks.length === 1 && !refs.segBlocks[0].total) {
      refs.segBlocks[0].fill.style.inlineSize = Math.min(100, done * 100 / total) + "%";
    }
  }
  if (!refs.segbar) renderMeta(nb);
  recomputeOverall();
}
function setRate(nb, speed, eta) {
  if (nb === null || nb === undefined) {
    const sp = humanSpeed(speed);
    dom.overallRate.textContent = sp ? t("overall_rate_left", { speed: sp, eta: formatEta(eta) || "—" }) : "";
    return;
  }
  const refs = epEls.get(nb); if (!refs) return;
  const sp = humanSpeed(speed), et = formatEta(eta);
  refs.rate.textContent = sp ? (et ? sp + " · " + et : sp) : "";
}

/* ----- segmented bars ---------------------------------------------- */
function buildSegbar(nb, totals) {
  const refs = epEls.get(nb); if (!refs) return;
  const tot = (totals && totals.length) ? totals : [0];
  refs.mid.innerHTML = "";
  const bar = document.createElement("div");
  bar.className = "segbar";
  const blocks = [];
  for (let k = 0; k < tot.length; k++) {
    const block = document.createElement("div"); block.className = "segblock";
    const fill = document.createElement("i"); block.appendChild(fill);
    bar.appendChild(block);
    blocks.push({ fill, total: tot[k] || 0 });
  }
  refs.mid.appendChild(bar);
  refs.segbar = bar; refs.segBlocks = blocks;
}
function setSegProgress(nb, k, done) {
  const refs = epEls.get(nb); if (!refs || !refs.segBlocks) return;
  const b = refs.segBlocks[k]; if (!b || !b.total) return;
  b.fill.style.inlineSize = Math.min(100, done * 100 / b.total) + "%";
}
function removeSeg(nb) {
  const refs = epEls.get(nb); if (!refs) return;
  refs.segbar = null; refs.segBlocks = null;
  renderMeta(nb);
}
function renderMeta(nb) {
  const refs = epEls.get(nb); if (!refs) return;
  const sz = epSize.get(nb);
  let txt = "";
  if (sz && sz.total) txt = humanSize(sz.done) + " / " + humanSize(sz.total);
  else if (sz && sz.done) txt = humanSize(sz.done);
  refs.mid.innerHTML = "";
  const m = document.createElement("div"); m.className = "ep-meta"; m.textContent = txt;
  refs.mid.appendChild(m); refs.meta = m;
}

/* ----- overall + complete counters --------------------------------- */
function recomputeOverall() {
  const sel = selectedNbs();
  let frac = 0, done = 0;
  for (const nb of sel) {
    const st = epStatus.get(nb);
    if (st === "done") { frac += 1; done += 1; }
    else if (st === "downloading" || st === "paused") frac += (epProgress.get(nb) || 0);
    // pending / error / queued / undefined contribute 0 — no stale fraction
  }
  const pct = sel.length ? Math.round(frac / sel.length * 100) : 0;
  dom.overallFill.style.width = pct + "%";
  dom.overallLabel.textContent = t("overall_progress", { pct });
  dom.epComplete.textContent = t("ep_complete", { done, total: sel.length });
}

function applyStatuses(map) {
  for (const nb in map) {
    const info = map[nb];
    if (!epEls.has(nb)) continue;
    if (info.done) setProgress(nb, info.done, info.total);
    setStatus(nb, info.status, { size: info.status === "done" ? info.total : 0 });
  }
}

/* ===== disk scan (downloaded vs not) =============================== */
let lastScanSummary = null;

// Apply a disk-scan result: mark downloaded/partial rows, un-mark rows the
// scan no longer reports (e.g. a file was deleted), auto-deselect downloaded
// episodes so Start only grabs the missing ones, and render the summary.
function applyScan(scan, summary) {
  scan = scan || {};
  resetRowsNotIn(scan);
  applyStatuses(scan);
  // Auto-deselect episodes found complete on disk.
  let touchedSeasons = false;
  for (const nb of allNbs) {
    const info = scan[nb];
    if (info && info.status === "done" && checked.get(nb)) {
      checked.set(nb, false);
      renderChk(nb);
      touchedSeasons = true;
    }
  }
  if (touchedSeasons) {
    for (const s of seasonEls.keys()) updateSeasonGlyph(s);
    recomputeOverall();
    scheduleSave();
  }
  renderScanSummary(summary);
}

// Reset to "pending" any row NOT present in the new scan map, so a rescan
// clears a previously-shown status when the file is gone. Never disturbs a
// live download (downloading/queued rows are left alone).
function resetRowsNotIn(scan) {
  for (const nb of allNbs) {
    if (nb in scan) continue;
    const st = epStatus.get(nb);
    if (st === "downloading" || st === "queued") continue;
    if (st && st !== "pending") {
      epProgress.set(nb, 0); epSize.delete(nb);
      setStatus(nb, "pending", {});
    }
  }
}

function renderScanSummary(summary) {
  lastScanSummary = summary || null;
  if (!dom.scanSummary) return;
  if (!summary) { dom.scanSummary.textContent = ""; return; }
  const missing = (summary.not_downloaded || 0) + (summary.partial || 0);
  // Integer counts only -> safe to wrap in colour spans via innerHTML.
  dom.scanSummary.innerHTML = t("scan_summary", {
    done: '<span class="scan-done">' + (summary.downloaded || 0) + "</span>",
    missing: '<span class="scan-miss">' + missing + "</span>",
  });
}

/* ===== run lifecycle / buttons ===================================== */
function updateButtons() {
  const hasPlan = !!state.plan;
  dom.fetchBtn.disabled = state.fetching;
  dom.pauseBtn.disabled = !state.running;
  dom.stopBtn.disabled = !state.running;
  dom.playBtn.disabled = !hasPlan;          // Play = Start (idle) or Resume (running)
}

function doFetch() {
  const raw = dom.url.value.trim();
  if (!raw) { toast(t("dlg_paste_url_first"), "error"); return; }
  state.fetching = true;
  updateButtons();
  appendLog(t("log_ui_fetch", { url: raw }), "action");
  py("fetch", raw);
}

function doStart() {
  if (!state.plan) return;
  if (!selectedNbs().length) { toast(t("dlg_no_episode"), "error"); return; }
  if (!state.dest) { toast(t("dlg_choose_dest"), "error"); return; }
  state.running = true;
  updateButtons();
  const qlabel = (dom.optQuality.selectedOptions[0] || {}).textContent || (selectedHeight() + "p");
  appendLog(t("log_ui_start", { n: selectedNbs().length, h: qlabel }), "action");
  py("start", {
    selected_nbs: selectedNbs(),
    height: selectedHeight(),
    dest: state.dest,
    concurrency: parseInt(dom.optConc.value, 10) || state.defaults.concurrency,
    segments: parseInt(dom.optSeg.value, 10) || state.defaults.segments,
    subtitle_langs: dom.optSubs.value || "ar",
  });
}

function onDone(summary) {
  state.running = false;
  updateButtons();
  dom.overallRate.textContent = "";
  // Global Stop leaves rows labelled "downloading" (no status event); settle them
  // so the bar drops to the honestly-completed ratio instead of freezing inflated.
  for (const nb of allNbs) {
    if (epStatus.get(nb) === "downloading") { epStatus.set(nb, "pending"); epProgress.set(nb, 0); }
  }
  recomputeOverall();
  if (summary) {
    const msg = t("log_run_summary", {
      done: summary.done || 0, total: summary.total || 0,
      error: summary.error || 0, paused: summary.paused || 0, pending: summary.pending || 0,
    });
    toast(msg, "info", 5000);
    appendLog(msg, "info");
    if ((summary.done || 0) >= (summary.total || 0) && summary.total) {
      toast(t("dlg_all_done"), "success", 4000);
    }
  }
  // Re-scan disk so the summary + selection reflect the just-finished files.
  py("scan").then((r) => { if (r) applyScan(r.scan, r.summary); });
}

/* ===== context menu ================================================= */
let ctxNb = null;
function openCtx(evt, nb) {
  if (!state.plan) return;
  ctxNb = nb;
  const menu = dom.ctxMenu;
  menu.classList.remove("hidden");
  const vw = window.innerWidth, vh = window.innerHeight;
  const rect = menu.getBoundingClientRect();
  let x = evt.clientX, y = evt.clientY;
  if (x + rect.width > vw) x = vw - rect.width - 6;
  if (y + rect.height > vh) y = vh - rect.height - 6;
  menu.style.left = x + "px";
  menu.style.top = y + "px";
}
function closeCtx() { dom.ctxMenu.classList.add("hidden"); ctxNb = null; }

/* ===== toast ======================================================= */
let toastTimer = null;
function toast(msg, type, ms) {
  if (!msg) return;
  const el = dom.toast;
  el.textContent = msg;
  el.className = "toast show" + (type ? " " + type : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.className = "toast"; }, ms || 3000);
}

/* ===== activity log ================================================= */
/* In-memory only: never persisted to session/save_state, so it starts empty
   on every launch (i.e. cleared on exit). A manual Clear button also empties it. */
const LOG = [];
const LOG_CAP = 1000;           // cap DOM/buffer growth on long sessions
function appendLog(msg, level) {
  if (!msg) return;
  const d = new Date();
  const time = [d.getHours(), d.getMinutes(), d.getSeconds()]
    .map((x) => String(x).padStart(2, "0")).join(":");
  LOG.push({ time, msg, level });
  if (dom.logEmpty) { dom.logEmpty.remove(); dom.logEmpty = null; }
  const row = document.createElement("div");
  row.className = "log-row" + (level ? " " + level : "");
  const ts = document.createElement("span");
  ts.className = "log-time"; ts.textContent = "[" + time + "]";
  const body = document.createElement("span");
  body.className = "log-msg"; body.textContent = msg;   // textContent = no HTML injection
  row.append(ts, body);
  dom.logList.appendChild(row);
  while (LOG.length > LOG_CAP) { LOG.shift(); dom.logList.firstChild.remove(); }
  if (state.view === "log") dom.logList.scrollTop = dom.logList.scrollHeight;
  dom.logCount.textContent = t("log_count", { n: LOG.length });
}
function clearLog() {
  LOG.length = 0;
  dom.logList.innerHTML = "";
  const e = document.createElement("div");
  e.className = "empty-state"; e.id = "log-empty";
  e.setAttribute("data-i18n", "log_empty"); e.textContent = t("log_empty");
  dom.logList.appendChild(e); dom.logEmpty = e;
  dom.logCount.textContent = "";
}
function logEpAction(key, nb) {
  const refs = epEls.get(nb);
  const label = refs ? refs.code.textContent : String(nb);
  appendLog(t(key, { label }), "action");
}

/* ===== event stream from Python ===================================== */
window.onAppEvent = function (events) {
  const list = Array.isArray(events) ? events : [events];
  for (const ev of list) {
    try { dispatch(ev); } catch (e) { console.error("dispatch", ev, e); }
  }
};
function dispatch(ev) {
  switch (ev.kind) {
    case "plan":
      state.fetching = false;
      renderPlan(ev.plan, null, null);
      updateButtons();
      break;
    case "hero":
      renderHero(ev.hero);
      break;
    case "scan":
      applyScan(ev.scan, ev.summary);
      break;
    case "fetch_error":
      state.fetching = false;
      updateButtons();
      toast(ev.err, "error", 5000);
      appendLog(ev.err, "error");
      break;
    case "log":
      toast(ev.msg, "info", 2400);
      appendLog(ev.msg, "info");
      break;
    case "status":
      setStatus(ev.nb, ev.status, ev.extra || {});
      break;
    case "progress":
      setProgress(ev.nb, ev.done, ev.total);
      break;
    case "segments":
      buildSegbar(ev.nb, ev.seg_totals);
      break;
    case "seg_progress":
      setSegProgress(ev.nb, ev.k, ev.done);
      break;
    case "rate":
      setRate(ev.nb, ev.speed, ev.eta);
      break;
    case "done":
      onDone(ev.summary);
      break;
  }
}

/* ===== view router ================================================= */
function showView(name) {
  if (!name) return;
  state.view = name;
  dom.viewDownload.classList.toggle("hidden", name !== "download");
  dom.viewLibrary.classList.toggle("hidden", name !== "library");
  dom.viewSettings.classList.toggle("hidden", name !== "settings");
  dom.viewLog.classList.toggle("hidden", name !== "log");
  dom.nav.querySelectorAll(".nav-item").forEach((el) => {
    el.classList.toggle("active", el.dataset.nav === name);
  });
  if (name === "library") loadLibrary();
  if (name === "log") dom.logList.scrollTop = dom.logList.scrollHeight;
}

/* ===== library ===================================================== */
async function loadLibrary() {
  const data = await py("get_library");
  state.libData = data || { dest: state.dest, series: [] };
  renderLibrary(state.libData);
}

function renderLibrary(data) {
  const list = dom.libList;
  list.innerHTML = "";
  const series = (data && data.series) || [];
  if (!series.length) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = t("library_empty");
    list.appendChild(empty);
    dom.libSummary.textContent = "";
    return;
  }

  let totalFiles = 0;
  for (const s of series) {
    const block = document.createElement("div");
    block.className = "lib-series collapsed";

    const head = document.createElement("div");
    head.className = "lib-series-head";
    const chevron = document.createElement("span");
    chevron.className = "lib-chevron";
    chevron.innerHTML = '<svg viewBox="0 0 14 14"><path d="M3.5 5 7 8.5 10.5 5" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>';
    const title = document.createElement("span");
    title.className = "lib-series-title";
    title.textContent = s.title || "—";
    const count = document.createElement("span");
    count.className = "lib-series-count";
    const c = s.counts || {};
    count.textContent = t("lib_done_count", { done: c.done || 0, total: c.total || s.episodes.length });
    head.append(chevron, title, count);
    head.addEventListener("click", () => block.classList.toggle("collapsed"));
    block.appendChild(head);

    const seriesBody = document.createElement("div");
    seriesBody.className = "lib-series-body";

    // group episodes by season (movies fall into a single ungrouped body)
    const bySeason = new Map();
    for (const ep of s.episodes) {
      totalFiles++;
      const key = ep.is_movie || ep.season == null ? "_" : ep.season;
      if (!bySeason.has(key)) bySeason.set(key, []);
      bySeason.get(key).push(ep);
    }
    for (const [season, eps] of bySeason) {
      const group = document.createElement("div");
      group.className = "season-group";
      if (season !== "_") {
        group.classList.add("collapsed");
        const header = document.createElement("div");
        header.className = "season-header";
        const chevron = document.createElement("span");
        chevron.className = "season-chevron";
        chevron.innerHTML = '<svg viewBox="0 0 14 14"><path d="M3.5 5 7 8.5 10.5 5" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>';
        const name = document.createElement("span");
        name.className = "season-name";
        name.textContent = t("season_label", { n: season });
        const cnt = document.createElement("span");
        cnt.className = "season-count";
        cnt.textContent = t("kind_episodes", { n: eps.length });
        header.append(chevron, name, cnt);
        header.addEventListener("click", () => group.classList.toggle("collapsed"));
        group.appendChild(header);
      }
      const body = document.createElement("div");
      body.className = "season-body";
      for (const ep of eps) body.appendChild(makeLibRow(ep));
      group.appendChild(body);
      seriesBody.appendChild(group);
    }
    block.appendChild(seriesBody);
    list.appendChild(block);
  }
  dom.libSummary.textContent = t("lib_summary", { series: series.length, files: totalFiles });
}

function makeLibRow(ep) {
  const row = document.createElement("div");
  row.className = "ep-row lib-row";

  const code = document.createElement("span");
  code.className = "ep-code";
  code.textContent = ep.is_movie ? t("movie_label") : (ep.label || "");

  const title = document.createElement("span");
  title.className = "ep-title";
  title.textContent = ep.title || "";

  const size = document.createElement("span");
  size.className = "lib-size";
  if (ep.total_bytes) size.textContent = humanSize(ep.total_bytes);
  else if (ep.downloaded_bytes) size.textContent = humanSize(ep.downloaded_bytes);

  const pill = document.createElement("span");
  pill.className = "pill " + ep.status;
  pill.textContent = t("status_" + ep.status);

  const actions = document.createElement("span");
  actions.className = "lib-actions";
  const open = document.createElement("button");
  open.className = "lib-btn"; open.dataset.act = "open"; open.dataset.path = ep.abs_path || "";
  open.textContent = t("btn_open_file"); open.disabled = !ep.exists;
  const folder = document.createElement("button");
  folder.className = "lib-btn"; folder.dataset.act = "folder"; folder.dataset.path = ep.abs_path || "";
  folder.textContent = t("btn_open_folder"); folder.disabled = !ep.abs_path;
  actions.append(open, folder);

  row.append(code, title, size, pill, actions);
  return row;
}

/* ===== settings ==================================================== */
function buildSettingsControls() {
  // default quality — the common ladder; selection = saved pref (or 1080)
  fillSelect(dom.setQuality, COMMON_HEIGHTS, (v) => v + "p",
    state.prefs.quality_height || 1080);
  fillSelect(dom.setConc, range(1, state.defaults.max_concurrency || 6),
    (n) => t("conc_files", { n }), state.prefs.concurrency);
  fillSelect(dom.setSeg, range(1, state.defaults.max_segments || 32),
    (n) => t("seg_per_file", { n }), state.prefs.segments);
  dom.setDestPath.textContent = state.dest || "—";
  dom.setDestPath.title = state.dest || "";
}

function rebuildSettingsLabels() {
  // The concurrency/segments option labels are language-dependent; re-fill them
  // keeping the current selection.
  if (!dom.setConc) return;
  fillSelect(dom.setConc, range(1, state.defaults.max_concurrency || 6),
    (n) => t("conc_files", { n }), parseInt(dom.setConc.value, 10) || state.prefs.concurrency);
  fillSelect(dom.setSeg, range(1, state.defaults.max_segments || 32),
    (n) => t("seg_per_file", { n }), parseInt(dom.setSeg.value, 10) || state.prefs.segments);
}

function onPrefChange() {
  const q = parseInt(dom.setQuality.value, 10) || null;
  const n = parseInt(dom.setConc.value, 10) || state.defaults.concurrency;
  const m = parseInt(dom.setSeg.value, 10) || state.defaults.segments;
  state.prefs.quality_height = q;
  state.prefs.concurrency = n;
  state.prefs.segments = m;
  // Live defaults so the next fresh fetch uses them immediately.
  state.defaults.concurrency = n;
  state.defaults.segments = m;
  py("save_prefs", { default_quality: q, default_concurrency: n, default_segments: m });
  appendLog(t("log_ui_prefs"), "action");
}

/* ===== window resize grips ========================================= */
/* The window is frameless, so the OS gives it no resize border. These grips
   (positioned over the physical window edges/corners in app.css) translate a
   pointer drag into Window.resize() calls on the Python side. Sizes are in CSS
   pixels, which is what pywebview's resize() expects. */
const RZ_MIN_W = 980, RZ_MIN_H = 680;   // keep in sync with webview_host.MIN_W/MIN_H

function wireResizeGrips() {
  let drag = null;          // active drag: {el, dir, sx, sy, w, h}
  let pending = null;       // latest target {w, h, fixEast, fixSouth} not yet sent
  let rafScheduled = false;
  let inFlight = false;     // a win_resize call is awaiting Python

  const clamp = (v, lo, hi) => Math.max(lo, Math.min(v, hi));

  function flush() {
    rafScheduled = false;
    if (!pending || inFlight) return;
    const p = pending; pending = null;
    inFlight = true;
    Promise.resolve(py("win_resize", p.w, p.h, p.fixEast, p.fixSouth)).then(() => {
      inFlight = false;
      if (pending) schedule();   // a newer size arrived while we were waiting
    }, () => { inFlight = false; });
  }
  function schedule() {
    if (rafScheduled) return;
    rafScheduled = true;
    requestAnimationFrame(flush);
  }

  function onMove(e) {
    if (!drag) return;
    // screenX/Y are absolute (independent of the window moving), so dragging
    // the west/north edge — which moves the window — never feeds back on itself.
    const dx = e.screenX - drag.sx;
    const dy = e.screenY - drag.sy;
    let w = drag.w, h = drag.h, fixEast = false, fixSouth = false;
    const d = drag.dir;
    if (d.includes("e")) w = drag.w + dx;
    if (d.includes("w")) { w = drag.w - dx; fixEast = true; }   // anchor right edge
    if (d.includes("s")) h = drag.h + dy;
    if (d.includes("n")) { h = drag.h - dy; fixSouth = true; }  // anchor bottom edge
    w = clamp(Math.round(w), RZ_MIN_W, screen.availWidth);
    h = clamp(Math.round(h), RZ_MIN_H, screen.availHeight);
    pending = { w, h, fixEast, fixSouth };
    schedule();
  }
  function onUp(e) {
    if (!drag) return;
    const el = drag.el;
    try { el.releasePointerCapture(e.pointerId); } catch (_) {}
    el.removeEventListener("pointermove", onMove);
    el.removeEventListener("pointerup", onUp);
    el.removeEventListener("pointercancel", onUp);
    drag = null;
    document.body.classList.remove("resizing");
    schedule();   // flush the final size
  }

  document.querySelectorAll(".rz").forEach((el) => {
    el.addEventListener("pointerdown", (e) => {
      if (e.button !== 0) return;
      e.preventDefault();
      drag = { el, dir: el.dataset.rz, sx: e.screenX, sy: e.screenY,
               w: window.innerWidth, h: window.innerHeight };
      document.body.classList.add("resizing");
      try { el.setPointerCapture(e.pointerId); } catch (_) {}
      el.addEventListener("pointermove", onMove);
      el.addEventListener("pointerup", onUp);
      el.addEventListener("pointercancel", onUp);
    });
  });
}

/* ===== static UI wiring ============================================ */
function wireStaticUI() {
  // language
  dom.langToggle.addEventListener("click", (e) => {
    const b = e.target.closest(".lang-opt"); if (!b) return;
    if (b.dataset.lang !== LANG) setLang(b.dataset.lang, true);
  });
  // window controls
  dom.winMin.addEventListener("click", () => py("win_minimize"));
  dom.winMax.addEventListener("click", () => py("win_maximize"));
  dom.winClose.addEventListener("click", () => py("win_close"));
  wireResizeGrips();

  // paste / fetch
  dom.pasteBtn.addEventListener("click", async () => {
    try {
      const txt = await navigator.clipboard.readText();
      if (txt) { dom.url.value = txt.trim(); scheduleSave(); }
    } catch (_) { dom.url.focus(); }
  });
  dom.fetchBtn.addEventListener("click", doFetch);
  dom.url.addEventListener("keydown", (e) => { if (e.key === "Enter") doFetch(); });
  dom.url.addEventListener("input", scheduleSave);

  // browse
  dom.browseBtn.addEventListener("click", async () => {
    const res = await py("browse_dest");
    if (res && res.dest) {
      state.dest = res.dest; setDestText(res.dest); updateDisk(res.disk); scheduleSave();
      if (res.scan) applyScan(res.scan, res.summary);
      appendLog(t("log_ui_dest", { dest: res.dest }), "action");
    }
  });

  // selection toolbar
  dom.rescanBtn.addEventListener("click", async () => {
    const r = await py("scan");
    if (r) applyScan(r.scan, r.summary);
  });
  dom.selAll.addEventListener("click", () => selectAllEpisodes(true));
  dom.selNone.addEventListener("click", () => selectAllEpisodes(false));
  dom.selInvert.addEventListener("click", invertSelection);

  // options
  dom.optQuality.addEventListener("change", scheduleSave);
  dom.optSubs.addEventListener("change", scheduleSave);
  dom.optConc.addEventListener("change", () => { updateConnWarn(); scheduleSave(); });
  dom.optSeg.addEventListener("change", () => { updateConnWarn(); scheduleSave(); });

  // footer controls
  dom.playBtn.addEventListener("click", () => {
    if (state.running) { appendLog(t("log_ui_resume_all"), "action"); py("resume_all"); }
    else doStart();
  });
  dom.pauseBtn.addEventListener("click", () => { appendLog(t("log_ui_pause_all"), "action"); py("pause_all"); });
  dom.stopBtn.addEventListener("click", () => { appendLog(t("log_ui_stop"), "action"); py("stop"); });

  // context menu
  dom.ctxMenu.addEventListener("click", (e) => {
    const item = e.target.closest(".ctx-item"); if (!item || !ctxNb) return;
    const act = item.dataset.act;
    if (act === "pause") { logEpAction("log_ui_ep_pause", ctxNb); py("pause", ctxNb); }
    else if (act === "resume") { logEpAction("log_ui_ep_resume", ctxNb); py("resume", ctxNb); }
    else if (act === "cancel") { logEpAction("log_ui_ep_cancel", ctxNb); py("cancel", ctxNb); }
    closeCtx();
  });
  document.addEventListener("click", (e) => { if (!e.target.closest(".ctx-menu")) closeCtx(); });
  document.addEventListener("scroll", closeCtx, true);
  window.addEventListener("blur", closeCtx);

  // nav: switch between Download / Library / Settings views
  dom.nav.addEventListener("click", (e) => {
    const item = e.target.closest(".nav-item"); if (!item) return;
    e.preventDefault();
    showView(item.dataset.nav);
  });

  // library
  dom.libRefresh.addEventListener("click", loadLibrary);
  dom.libList.addEventListener("click", (e) => {
    const btn = e.target.closest(".lib-btn"); if (!btn || btn.disabled) return;
    const path = btn.dataset.path; if (!path) return;
    py(btn.dataset.act === "folder" ? "reveal_path" : "open_path", path);
  });

  // settings
  dom.setLangToggle.addEventListener("click", (e) => {
    const b = e.target.closest(".lang-opt"); if (!b) return;
    if (b.dataset.lang !== LANG) setLang(b.dataset.lang, true);
  });
  dom.setBrowseBtn.addEventListener("click", async () => {
    const res = await py("browse_dest");
    if (res && res.dest) {
      state.dest = res.dest;
      setDestText(res.dest);
      dom.setDestPath.textContent = res.dest;
      dom.setDestPath.title = res.dest;
      updateDisk(res.disk);
      if (res.scan) applyScan(res.scan, res.summary);
      scheduleSave();
      appendLog(t("log_ui_dest", { dest: res.dest }), "action");
    }
  });
  dom.setQuality.addEventListener("change", onPrefChange);
  dom.setConc.addEventListener("change", onPrefChange);
  dom.setSeg.addEventListener("change", onPrefChange);

  // log
  dom.logClear.addEventListener("click", clearLog);
}

/* ===== init ======================================================== */
function init() {
  cacheDom();
  STRINGS = EXTRA_STRINGS;          // until bootstrap merges the engine table
  wireStaticUI();
  whenPywebviewReady(boot);
}
function whenPywebviewReady(cb) {
  if (window.pywebview && window.pywebview.api) cb();
  else window.addEventListener("pywebviewready", cb, { once: true });
}
async function boot() {
  try {
    const data = await window.pywebview.api.get_bootstrap();
    applyBootstrap(data);
  } catch (e) {
    console.error("bootstrap failed", e);
  }
  py("ui_ready");
}

if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
else init();
