// Drives the PWA in headless Chromium against an S3 server (run by test_pwa.py).
// usage: node pwa_e2e.mjs <app url> <bundle text> <passphrase> <screenshot dir>
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { chromium } = require("playwright");

const [appUrl, bundle, passphrase, shots] = process.argv.slice(2);
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2 });
const errors = [];
page.on("pageerror", (e) => errors.push(String(e)));
page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });

const result = {};
const r2Requests = [];
page.on("request", (req) => {
  const url = req.url();
  if (req.method() === "GET" && /^https?:/.test(url) && !url.startsWith(appUrl)) r2Requests.push(new URL(req.url()).pathname + new URL(req.url()).search);
});
const visible = (id) => page.locator(`#${id}`).isVisible();
const loadedThumbs = () => page.$$eval(".tile img", (imgs) => imgs.filter((i) => i.complete && i.naturalWidth > 0).length);

try {
  await page.goto(appUrl);
  result.firstScreen = (await visible("setup")) ? "setup" : "other";
  await page.screenshot({ path: `${shots}/1-setup.png` });

  // Wrong passphrase is rejected and nothing is stored.
  await page.fill("#bundle-input", bundle);
  await page.fill("#setup-passphrase", "wrong passphrase!");
  await page.click("#setup-submit");
  await page.waitForFunction(() => document.querySelector("#setup-error").textContent.length > 0);
  result.wrongPassError = await page.textContent("#setup-error");

  await page.fill("#setup-passphrase", passphrase);
  await page.click("#setup-submit");
  await page.waitForSelector("#gallery:not([hidden])");
  await page.waitForFunction(() => document.querySelectorAll(".folder").length > 0);
  await page.waitForFunction(() => [...document.querySelectorAll(".folder img")]
    .filter((i) => i.complete && i.naturalWidth > 0).length >= 3, null, { timeout: 15000 });
  result.folders = await page.$$eval(".folder", (f) => f.map((x) => [x.querySelector(".name").textContent, x.querySelector(".n").textContent]));
  result.coversLoaded = await page.$$eval(".folder img", (imgs) => imgs.filter((i) => i.complete && i.naturalWidth > 0).length);
  result.folderCount = await page.textContent("#count");
  await page.screenshot({ path: `${shots}/2-folders.png` });
  await page.selectOption("#site-filter", { index: 2 });
  result.sitesFolders = await page.textContent("#count");
  await page.selectOption("#site-filter", { index: 0 });

  // Into a folder: its photos in page order.
  await page.click(".folder >> nth=0");
  await page.waitForFunction(() => [...document.querySelectorAll(".tile img")].filter((i) => i.complete && i.naturalWidth > 0).length >= 3);
  result.inFolder = {
    title: await page.textContent("#folder-title"), count: await page.textContent("#count"),
    tiles: await page.$$eval(".tile", (t) => t.length), thumbs: await loadedThumbs(), back: await visible("back-button"),
  };
  await page.screenshot({ path: `${shots}/3-folder.png` });

  // Full-size viewer.
  await page.click(".tile >> nth=0");
  await page.waitForFunction(() => { const i = document.querySelector("#viewer-img"); return i.complete && i.naturalWidth > 0; });
  result.viewer = await page.$eval("#viewer-img", (i) => ({ w: i.naturalWidth, h: i.naturalHeight }));
  result.viewerTitle = await page.textContent("#viewer-title");
  result.viewerMeta = await page.textContent("#viewer-meta");
  await page.screenshot({ path: `${shots}/4-viewer.png` });
  await page.click("#viewer-next");
  await page.waitForFunction((t) => document.querySelector("#viewer-title").textContent !== t, result.viewerTitle);
  result.nextTitle = await page.textContent("#viewer-title");

  // Back (Android back button) closes the viewer, then leaves the folder.
  await page.goBack();
  await page.waitForFunction(() => !document.querySelector("#viewer").open);
  result.afterBack = { viewerOpen: await page.$eval("#viewer", (d) => d.open), inFolder: await visible("folder-title") };
  await page.goBack();
  await page.waitForFunction(() => document.querySelectorAll(".folder").length > 0);
  result.afterSecondBack = { folders: await page.$$eval(".folder", (f) => f.length), back: await visible("back-button") };

  // All photos, newest first, with the site filter.
  await page.click("#view-toggle");
  await page.waitForFunction(() => document.querySelectorAll(".tile").length === 10);
  await page.waitForFunction(() => [...document.querySelectorAll(".tile img")].filter((i) => i.complete && i.naturalWidth > 0).length >= 9);
  result.all = {
    tiles: await page.$$eval(".tile", (t) => t.length), thumbs: await loadedThumbs(),
    placeholders: await page.$$eval(".tile:not(:has(img))", (t) => t.map((x) => x.textContent)),
    count: await page.textContent("#count"),
  };
  result.sites = await page.$$eval("#site-filter option", (o) => o.map((x) => x.textContent));
  await page.selectOption("#site-filter", { index: 2 });
  result.filteredCount = await page.textContent("#count");
  await page.selectOption("#site-filter", { index: 0 });
  await page.click(".tile >> nth=0");
  await page.waitForFunction(() => document.querySelector("#viewer").open);
  await page.click("#viewer-close");
  await page.waitForFunction(() => !document.querySelector("#viewer").open);

  // Lock wipes the gallery; the sealed bundle survives a reload and unlocks with the passphrase.
  await page.click("#lock-button");
  result.afterLock = { lockVisible: await visible("lock"), tiles: await page.$$eval(".tile", (t) => t.length) };
  await page.reload();
  result.afterReload = (await visible("lock")) ? "lock" : "other";
  await page.screenshot({ path: `${shots}/4-lock.png` });
  r2Requests.length = 0;
  await page.fill("#lock-passphrase", passphrase);
  await page.press("#lock-passphrase", "Enter");
  await page.waitForFunction(() => document.querySelectorAll(".tile img").length >= 9);
  result.reunlockedTiles = await page.$$eval(".tile", (t) => t.length);
  result.requestsOnReopen = [...r2Requests];
  result.storedBundleIsSealed = await page.evaluate(() => new Promise((resolve) => {
    const req = indexedDB.open("image-vault");
    req.onsuccess = () => {
      const get = req.result.transaction("settings").objectStore("settings").get("bundle");
      get.onsuccess = () => resolve(get.result.startsWith("ICVIEW1."));
    };
  }));
  result.serviceWorker = await page.evaluate(async () => !!(await navigator.serviceWorker.getRegistration()));
} catch (err) {
  const state = await page.evaluate(() => ({
    screens: ["setup", "lock", "gallery"].filter((id) => !document.getElementById(id).hidden),
    galleryStatus: document.getElementById("gallery-status").textContent,
    setupError: document.getElementById("setup-error").textContent,
    lockError: document.getElementById("lock-error").textContent,
  })).catch(() => ({}));
  await page.screenshot({ path: `${shots}/failure.png` }).catch(() => {});
  console.error(JSON.stringify({ failure: String(err), state, errors }, null, 1));
  await browser.close();
  process.exit(1);
}
result.errors = errors;
await browser.close();
console.log(JSON.stringify(result));
