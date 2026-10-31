// IndexedDB persistence. Stored: the passphrase-sealed bundle, and a cache of *encrypted*
// blobs (index segments and thumbnails, exactly as downloaded). Never keys or plaintext.

const DB_NAME = "image-vault";
const SETTINGS = "settings";
const BLOBS = "blobs";
const BUNDLE = "bundle";
const MAX_CACHED_BLOBS = 8000; // ~130 MB of thumbnails at ~16 KB each

function open() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, 2);
    req.onupgradeneeded = (event) => {
      const db = req.result;
      if (event.oldVersion < 1) db.createObjectStore(SETTINGS);
      if (event.oldVersion < 2) db.createObjectStore(BLOBS, { keyPath: "key" }).createIndex("t", "t");
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

async function run(storeName, mode, fn) {
  const db = await open();
  try {
    return await new Promise((resolve, reject) => {
      const tx = db.transaction(storeName, mode);
      const req = fn(tx.objectStore(storeName));
      tx.oncomplete = () => resolve(req?.result);
      tx.onerror = () => reject(tx.error);
    });
  } finally {
    db.close();
  }
}

export const loadBundle = () => run(SETTINGS, "readonly", (s) => s.get(BUNDLE)).catch(() => undefined);
export const saveBundle = (text) => run(SETTINGS, "readwrite", (s) => s.put(text, BUNDLE));
export const forgetBundle = () => run(SETTINGS, "readwrite", (s) => s.delete(BUNDLE));

// --- encrypted blob cache (objects are immutable, so a cached copy never goes stale) ---

export const getBlob = (key) =>
  run(BLOBS, "readonly", (s) => s.get(key)).then((row) => row?.data).catch(() => undefined);

export async function putBlob(key, data) {
  await run(BLOBS, "readwrite", (s) => s.put({ key, data, t: Date.now() })).catch(() => {});
  const count = await run(BLOBS, "readonly", (s) => s.count()).catch(() => 0);
  if (count > MAX_CACHED_BLOBS) await evictOldest(count - MAX_CACHED_BLOBS);
}

function evictOldest(n) {
  return run(BLOBS, "readwrite", (s) => {
    const cursor = s.index("t").openCursor();
    cursor.onsuccess = () => {
      if (cursor.result && n-- > 0) {
        cursor.result.delete();
        cursor.result.continue();
      }
    };
    return cursor;
  }).catch(() => {});
}

export const cachedKeys = (prefix) =>
  run(BLOBS, "readonly", (s) => s.getAllKeys(IDBKeyRange.bound(prefix, prefix + "￿"))).catch(() => []);

export const deleteBlobs = (keys) =>
  run(BLOBS, "readwrite", (s) => { for (const k of keys) s.delete(k); }).catch(() => {});

export const clearBlobs = () => run(BLOBS, "readwrite", (s) => s.clear()).catch(() => {});
