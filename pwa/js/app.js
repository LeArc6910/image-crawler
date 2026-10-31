// Viewer UI: unlock → load index → thumbnail grid → full image. Everything is decrypted in this
// page; only ciphertext ever crosses the network, and only the sealed bundle is persisted.

import * as db from "./db.js";
import { BundleError, looksLikeBundle, openBundle } from "./keystore.js";
import { BudgetExceeded, RequestBudget, S3Client, S3Error } from "./s3.js";
import { scanQr } from "./scanner.js";
import { INDEX_PREFIX, KIND_IMAGE, KIND_THUMB, imageKey, openBlob, readIndex, thumbKey } from "./vault.js";

const $ = (id) => document.getElementById(id);
const IDLE_LOCK_MS = 10 * 60 * 1000;
const HIDDEN_LOCK_MS = 60 * 1000;
const THUMB_CONCURRENCY = 6;

let session = null; // { s3, key } while unlocked
let entries = [];   // index entries, newest first
let shown = [];     // photos in the grid on screen (a folder, or all after the site filter)
const objectUrls = new Set();
const budget = new RequestBudget(); // shared across unlocks, so locking doesn't reset it

// --- screens ---------------------------------------------------------------

function show(id) {
  for (const s of ["setup", "lock", "gallery"]) $(s).hidden = s !== id;
}

async function start() {
  if ("serviceWorker" in navigator) navigator.serviceWorker.register("sw.js").catch(() => {});
  show((await db.loadBundle()) ? "lock" : "setup");
}

function describeError(err) {
  if (err instanceof BundleError) return err.message === "wrong passphrase or damaged bundle"
    ? "비밀번호가 틀렸거나 연결 정보가 손상됐습니다." : err.message;
  if (err instanceof S3Error) {
    if (err.status === 403) return "R2가 접근을 거부했습니다. 휴대폰용 토큰이 유효한지 확인하세요.";
    if (err.status === 404) return "버킷이나 파일을 찾을 수 없습니다.";
    return `R2 오류: ${err.message}`;
  }
  if (err instanceof BudgetExceeded) {
    return `짧은 시간에 요청이 너무 많아 멈췄습니다(요금 보호). ${Math.ceil(err.retryInMs / 60000)}분 뒤 다시 시도하세요.`;
  }
  if (err instanceof TypeError) return "R2에 연결할 수 없습니다. 네트워크나 버킷 CORS 설정(image-crawler cors)을 확인하세요.";
  return err?.message || String(err);
}

async function busy(button, fn) {
  button.disabled = true;
  try { return await fn(); } finally { button.disabled = false; }
}

async function unlock(text, passphrase, errorEl) {
  errorEl.textContent = "";
  const from = errorEl.closest(".screen").id;
  try {
    const { config, encryptionKey } = await openBundle(text, passphrase);
    session = { s3: new S3Client(config, { budget }), key: encryptionKey };
    view = { mode: readMode(), folder: null };
    history.replaceState(null, "");
    show("gallery");
    resetIdleTimer();
    await loadGallery();
    return true;
  } catch (err) {
    session = null;
    show(from);
    errorEl.textContent = describeError(err);
    return false;
  }
}

function lock() {
  session = null;
  entries = shown = folders = [];
  pendingTiles = [];
  view = { mode: view.mode, folder: null };
  scrollFor.clear();
  $("viewer").close();
  $("grid").replaceChildren();
  for (const url of objectUrls) URL.revokeObjectURL(url);
  objectUrls.clear();
  $("lock-passphrase").value = "";
  show("lock");
}

// --- setup / lock ------------------------------------------------------------

$("setup-submit").addEventListener("click", () => busy($("setup-submit"), async () => {
  const text = $("bundle-input").value.trim();
  if (!looksLikeBundle(text)) {
    $("setup-error").textContent = "ICVIEW1. 로 시작하는 연결 정보를 넣어 주세요.";
    return;
  }
  if (await unlock(text, $("setup-passphrase").value, $("setup-error"))) {
    await db.saveBundle(text);
    $("bundle-input").value = $("setup-passphrase").value = "";
  }
}));

$("lock-submit").addEventListener("click", () => busy($("lock-submit"), async () => {
  const text = await db.loadBundle();
  if (!text) return show("setup");
  if (await unlock(text, $("lock-passphrase").value, $("lock-error"))) $("lock-passphrase").value = "";
}));

for (const [input, button] of [["setup-passphrase", "setup-submit"], ["lock-passphrase", "lock-submit"]]) {
  $(input).addEventListener("keydown", (e) => { if (e.key === "Enter") $(button).click(); });
}

$("forget-button").addEventListener("click", async () => {
  if (!confirm("이 기기에 저장된 연결 정보를 지울까요? 다시 QR을 스캔해야 합니다.")) return;
  await db.forgetBundle();
  await db.clearBlobs();
  show("setup");
});

let scanAbort = null;
$("scan-button").addEventListener("click", async () => {
  scanAbort = new AbortController();
  $("scanner").hidden = false;
  $("scan-button").hidden = true;
  $("setup-error").textContent = "";
  try {
    $("bundle-input").value = await scanQr($("scanner-video"), looksLikeBundle, scanAbort.signal);
    $("setup-passphrase").focus();
  } catch (err) {
    if (err.name !== "AbortError") $("setup-error").textContent = `카메라를 쓸 수 없습니다: ${describeError(err)}`;
  } finally {
    $("scanner").hidden = true;
    $("scan-button").hidden = false;
  }
});
$("scan-cancel").addEventListener("click", () => scanAbort?.abort());

// --- gallery -----------------------------------------------------------------

/** Index segments and thumbnails never change once written, so a cached ciphertext is always valid. */
async function cachedGet(key) {
  const hit = await db.getBlob(key);
  if (hit) return hit;
  const blob = await session.s3.get(key);
  await db.putBlob(key, blob);
  return blob;
}

const siteOf = (e) => { try { return new URL(e.site).host; } catch { return "알 수 없음"; } };
const RENDER_CHUNK = 240;
const OTHER_FOLDER = "기타";

/** Images are grouped by the page they came from; the crawler names that folder (album title). */
function folderKeyOf(e) { return e.page_url || ""; }
function folderTitleOf(e) {
  if (e.album) return e.album;
  try { const u = new URL(e.page_url); return decodeURIComponent(u.pathname + u.search) || u.host; } catch { return OTHER_FOLDER; }
}

let folders = [];       // [{ key, title, site, items: [entries in page order], newest }]
let view = { mode: "folders", folder: null }; // mode: "folders" | "all"; folder: key when inside one
const scrollFor = new Map(); // view → scroll position, restored when coming back

function readMode() { try { return localStorage.getItem("view-mode") === "all" ? "all" : "folders"; } catch { return "folders"; } }
function saveMode(mode) { try { localStorage.setItem("view-mode", mode); } catch { /* private mode */ } }

async function loadGallery() {
  const status = $("gallery-status");
  status.textContent = "목록을 불러오는 중…";
  $("grid").replaceChildren();
  const { s3, key } = session;
  const segmentKeys = await s3.list(INDEX_PREFIX);
  const live = new Set(segmentKeys);
  await db.deleteBlobs((await db.cachedKeys(INDEX_PREFIX)).filter((k) => !live.has(k))); // compacted away
  const segments = [];
  for (const k of segmentKeys) segments.push({ key: k, blob: await cachedGet(k) });
  entries = (await readIndex(key, segments)).reverse(); // newest first
  entries.sort((a, b) => (b.fetched_at || "").localeCompare(a.fetched_at || ""));

  const byKey = new Map();
  for (const e of entries) {
    const k = folderKeyOf(e);
    let f = byKey.get(k);
    if (!f) byKey.set(k, f = { key: k, title: folderTitleOf(e), site: siteOf(e), items: [], newest: e.fetched_at || "" });
    f.items.push(e);
  }
  folders = [...byKey.values()]; // newest folder first (entries are sorted newest first)
  for (const f of folders) {
    f.items.sort((a, b) => (a.position ?? 1e9) - (b.position ?? 1e9) || (a.fetched_at || "").localeCompare(b.fetched_at || ""));
  }

  const counts = new Map();
  for (const e of entries) counts.set(siteOf(e), (counts.get(siteOf(e)) || 0) + 1);
  const select = $("site-filter");
  const previous = select.value;
  select.replaceChildren(new Option(`모든 사이트 (${entries.length})`, ""),
    ...[...counts].sort().map(([site, n]) => new Option(`${site} (${n})`, site)));
  select.value = counts.has(previous) ? previous : "";
  if (view.folder !== null && !byKey.has(view.folder)) view.folder = null;
  status.textContent = entries.length ? "" : "아직 저장된 이미지가 없습니다.";
  render();
}

function render() {
  const site = $("site-filter").value;
  const inFolder = view.folder !== null;
  const folder = inFolder ? folders.find((f) => f.key === view.folder) : null;
  $("back-button").hidden = !inFolder;
  $("site-filter").hidden = inFolder;
  $("folder-title").hidden = !inFolder;
  $("view-toggle").hidden = inFolder;
  $("view-toggle").textContent = view.mode === "all" ? "폴더" : "전체 사진";
  thumbQueue.length = 0;

  if (inFolder) {
    $("folder-title").textContent = folder.title;
    shown = folder.items;
    $("count").textContent = `${shown.length}장`;
    renderTiles(shown.map((entry, i) => () => photoTile(entry, i)), "grid");
  } else if (view.mode === "all") {
    shown = site ? entries.filter((e) => siteOf(e) === site) : entries;
    $("count").textContent = `${shown.length}장`;
    renderTiles(shown.map((entry, i) => () => photoTile(entry, i)), "grid");
  } else {
    shown = [];
    const list = site ? folders.filter((f) => f.site === site) : folders;
    $("count").textContent = `폴더 ${list.length}개`;
    renderTiles(list.map((f) => () => folderTile(f)), "grid folders");
  }
  const y = scrollFor.get(viewId()) || 0;
  requestAnimationFrame(() => scrollTo(0, y));
}

const viewId = () => view.folder !== null ? `f:${view.folder}` : `${view.mode}:${$("site-filter").value}`;

/** Tiles are built in chunks as the end of the grid scrolls into view (thousands of folders). */
let pendingTiles = [];
const moreObserver = new IntersectionObserver((items) => {
  if (items.some((i) => i.isIntersecting)) appendTiles();
}, { rootMargin: "800px" });

function renderTiles(makers, className) {
  const grid = $("grid");
  grid.className = className;
  grid.replaceChildren();
  pendingTiles = makers;
  appendTiles();
}

function appendTiles(minCount = 0) {
  const grid = $("grid");
  moreObserver.disconnect();
  const n = Math.max(RENDER_CHUNK, minCount);
  grid.append(...pendingTiles.splice(0, n).map((make) => make()));
  if (pendingTiles.length && grid.lastElementChild) moreObserver.observe(grid.lastElementChild);
}

const thumbFor = new WeakMap(); // element → entry whose thumbnail goes into it

function watchThumb(el, entry) {
  if (!entry.thumb) {
    el.textContent = (entry.content_type || "").replace(/^image\/|\+.*$/g, "").toUpperCase() || "?";
    return;
  }
  thumbFor.set(el, entry);
  thumbObserver.observe(el);
}

function photoTile(entry, i) {
  const tile = document.createElement("button");
  tile.className = "tile";
  tile.type = "button";
  tile.setAttribute("aria-label", entry.filename || "이미지");
  watchThumb(tile, entry);
  tile.addEventListener("click", () => openViewer(i));
  return tile;
}

function folderTile(folder) {
  const tile = document.createElement("button");
  tile.className = "folder";
  tile.type = "button";
  const cover = document.createElement("span");
  cover.className = "cover";
  const name = document.createElement("span");
  name.className = "name";
  name.textContent = folder.title;
  const count = document.createElement("span");
  count.className = "n";
  count.textContent = `${folder.items.length}`;
  tile.append(cover, name, count);
  tile.setAttribute("aria-label", `${folder.title} (${folder.items.length}장)`);
  watchThumb(cover, folder.items.find((e) => e.thumb) || folder.items[0]);
  tile.addEventListener("click", () => openFolder(folder.key));
  return tile;
}

function openFolder(key) {
  scrollFor.set(viewId(), scrollY);
  history.pushState({ folder: key }, "");
  view.folder = key;
  scrollFor.delete(viewId());
  render();
}

$("back-button").addEventListener("click", () => history.back());
$("view-toggle").addEventListener("click", () => {
  scrollFor.set(viewId(), scrollY);
  view.mode = view.mode === "all" ? "folders" : "all";
  saveMode(view.mode);
  render();
});
$("site-filter").addEventListener("change", render);
$("refresh-button").addEventListener("click", () => busy($("refresh-button"), () =>
  loadGallery().catch((err) => { $("gallery-status").textContent = describeError(err); })));
$("lock-button").addEventListener("click", lock);

// Android back / browser back: closes the viewer first, then leaves the folder.
addEventListener("popstate", (e) => {
  if (!session) return;
  const state = e.state || {};
  if (!state.viewer && $("viewer").open) $("viewer").close();
  const folder = state.folder ?? null;
  if (folder !== view.folder) {
    if (view.folder !== null) scrollFor.delete(viewId());
    view.folder = folders.some((f) => f.key === folder) ? folder : null;
    render();
  }
});

// Thumbnails load as tiles scroll into view, a few at a time.
const thumbQueue = [];
let thumbsInFlight = 0;

const thumbObserver = new IntersectionObserver((items) => {
  for (const item of items) {
    if (!item.isIntersecting) continue;
    thumbObserver.unobserve(item.target);
    thumbQueue.push(item.target);
  }
  pumpThumbs();
}, { rootMargin: "400px" });

function pumpThumbs() {
  while (session && thumbsInFlight < THUMB_CONCURRENCY && thumbQueue.length) {
    const el = thumbQueue.shift();
    const entry = thumbFor.get(el);
    if (!entry || !el.isConnected) continue;
    thumbsInFlight++;
    loadThumb(el, entry).finally(() => { thumbsInFlight--; pumpThumbs(); });
  }
}

async function loadThumb(el, entry) {
  const current = session;
  try {
    const blob = await cachedGet(thumbKey(entry.thumb));
    const { content } = await openBlob(current.key, blob, { kind: KIND_THUMB, name: entry.thumb });
    if (session !== current) return;
    const img = new Image();
    img.alt = "";
    img.src = trackUrl(new Blob([content], { type: "image/webp" }));
    el.replaceChildren(img);
  } catch (err) {
    if (err instanceof BudgetExceeded) $("gallery-status").textContent = describeError(err);
    el.classList.add("failed");
    el.textContent = "!";
    el.title = describeError(err);
  }
}

function trackUrl(blob) {
  const url = URL.createObjectURL(blob);
  objectUrls.add(url);
  return url;
}

// --- full-size viewer ----------------------------------------------------------

let viewerIndex = -1;
let viewerAbort = null;
let viewerFile = null; // { blob, filename } for save/share

const formatBytes = (n) => n >= 1e6 ? `${(n / 1e6).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1e3))} KB`;

async function openViewer(i) {
  if (i < 0 || i >= shown.length || !session) return;
  viewerIndex = i;
  viewerAbort?.abort();
  viewerAbort = new AbortController();
  const entry = shown[i];
  const img = $("viewer-img");
  if (img.src) { URL.revokeObjectURL(img.src); objectUrls.delete(img.src); }
  img.removeAttribute("src");
  viewerFile = null;
  $("viewer-title").textContent = entry.filename || entry.object.slice(0, 12);
  $("viewer-meta").textContent = [
    siteOf(entry),
    entry.width && entry.height ? `${entry.width}×${entry.height}` : null,
    entry.size ? formatBytes(entry.size) : null,
    entry.fetched_at ? new Date(entry.fetched_at).toLocaleString("ko-KR") : null,
  ].filter(Boolean).join(" · ");
  const source = $("viewer-source");
  source.textContent = entry.source_url || "";
  source.href = entry.source_url || "#";
  $("viewer-prev").disabled = i === 0;
  $("viewer-next").disabled = i === shown.length - 1;
  $("viewer-status").textContent = "원본을 불러오는 중…";
  if (!$("viewer").open) {
    history.pushState({ ...(history.state || {}), viewer: true }, "");
    $("viewer").showModal();
  }

  const current = session;
  try {
    const blob = await current.s3.get(imageKey(entry.object), { signal: viewerAbort.signal });
    const { content, metadata } = await openBlob(current.key, blob, { kind: KIND_IMAGE, name: entry.object });
    if (viewerIndex !== i || session !== current) return;
    const file = new Blob([content], { type: metadata.content_type || entry.content_type || "application/octet-stream" });
    viewerFile = { blob: file, filename: metadata.filename || entry.filename || `${entry.object.slice(0, 12)}.img` };
    img.src = trackUrl(file);
    $("viewer-status").textContent = "";
  } catch (err) {
    if (err.name !== "AbortError" && viewerIndex === i) $("viewer-status").textContent = describeError(err);
  }
}

$("viewer-prev").addEventListener("click", () => openViewer(viewerIndex - 1));
$("viewer-next").addEventListener("click", () => openViewer(viewerIndex + 1));
$("viewer-close").addEventListener("click", () => $("viewer").close());
$("viewer").addEventListener("close", () => {
  viewerAbort?.abort();
  if (session && history.state?.viewer) history.back(); // closed by button/Esc: drop its history entry
});
$("viewer").addEventListener("keydown", (e) => {
  if (e.key === "ArrowLeft") openViewer(viewerIndex - 1);
  if (e.key === "ArrowRight") openViewer(viewerIndex + 1);
});

let touchX = null;
$("viewer").addEventListener("touchstart", (e) => { touchX = e.touches.length === 1 ? e.touches[0].clientX : null; });
$("viewer").addEventListener("touchend", (e) => {
  if (touchX === null) return;
  const dx = e.changedTouches[0].clientX - touchX;
  if (Math.abs(dx) > 60) openViewer(viewerIndex + (dx < 0 ? 1 : -1));
  touchX = null;
});

$("viewer-save").addEventListener("click", () => {
  if (!viewerFile) return;
  const a = document.createElement("a");
  a.href = URL.createObjectURL(viewerFile.blob);
  a.download = viewerFile.filename;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 10_000);
});

if (navigator.canShare) {
  $("viewer-share").hidden = false;
  $("viewer-share").addEventListener("click", async () => {
    if (!viewerFile) return;
    const file = new File([viewerFile.blob], viewerFile.filename, { type: viewerFile.blob.type });
    if (navigator.canShare({ files: [file] })) await navigator.share({ files: [file] }).catch(() => {});
  });
}

// --- auto-lock -------------------------------------------------------------------

let idleTimer = null;
let hiddenTimer = null;

function resetIdleTimer() {
  clearTimeout(idleTimer);
  if (session) idleTimer = setTimeout(lock, IDLE_LOCK_MS);
}
for (const ev of ["pointerdown", "keydown", "scroll", "touchstart"]) {
  addEventListener(ev, resetIdleTimer, { passive: true, capture: true });
}
document.addEventListener("visibilitychange", () => {
  clearTimeout(hiddenTimer);
  if (document.hidden && session) hiddenTimer = setTimeout(lock, HIDDEN_LOCK_MS);
});

start();
