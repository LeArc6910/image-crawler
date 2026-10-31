// IndexedDB persistence. Only the passphrase-sealed bundle text is stored — never keys or images.

const DB_NAME = "image-vault";
const STORE = "settings";
const BUNDLE = "bundle";

function open() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, 1);
    req.onupgradeneeded = () => req.result.createObjectStore(STORE);
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

async function run(mode, fn) {
  const db = await open();
  try {
    return await new Promise((resolve, reject) => {
      const tx = db.transaction(STORE, mode);
      const req = fn(tx.objectStore(STORE));
      tx.oncomplete = () => resolve(req.result);
      tx.onerror = () => reject(tx.error);
    });
  } finally {
    db.close();
  }
}

export const loadBundle = () => run("readonly", (s) => s.get(BUNDLE)).catch(() => undefined);
export const saveBundle = (text) => run("readwrite", (s) => s.put(text, BUNDLE));
export const forgetBundle = () => run("readwrite", (s) => s.delete(BUNDLE));
