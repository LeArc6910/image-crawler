// Decrypt an archive written by the Python crawler using the PWA's vault.js (run by test_js_vault.py).
// usage: node check_vault.mjs <storage root> <master key, base64>
import { createHash } from "node:crypto";
import { readdir, readFile } from "node:fs/promises";
import { join } from "node:path";
import {
  KIND_IMAGE, KIND_THUMB, deriveEncryptionKey, imageKey, openBlob, readIndex, thumbKey,
} from "../../pwa/js/vault.js";

const [root, masterB64] = process.argv.slice(2);
const key = await deriveEncryptionKey(Buffer.from(masterB64, "base64"));

const segments = [];
for (const file of await readdir(join(root, "index"))) {
  segments.push({ key: `index/${file}`, blob: await readFile(join(root, "index", file)) });
}
const entries = await readIndex(key, segments);

const images = [];
for (const entry of entries) {
  const image = await openBlob(key, await readFile(join(root, imageKey(entry.object))),
    { kind: KIND_IMAGE, name: entry.object });
  let thumbBytes = null;
  if (entry.thumb) {
    const thumb = await openBlob(key, await readFile(join(root, thumbKey(entry.thumb))),
      { kind: KIND_THUMB, name: entry.thumb });
    thumbBytes = thumb.content.length;
  }
  images.push({
    object: entry.object,
    sha256: createHash("sha256").update(image.content).digest("hex"),
    source_url: image.metadata.source_url,
    thumbBytes,
  });
}

// A thumbnail blob served in place of its image must be rejected (slot binding).
let swapRejected = false;
const withThumb = entries.find((e) => e.thumb);
if (withThumb) {
  try {
    await openBlob(key, await readFile(join(root, thumbKey(withThumb.thumb))),
      { kind: KIND_IMAGE, name: withThumb.object });
  } catch {
    swapRejected = true;
  }
}

console.log(JSON.stringify({ entries, images, swapRejected }));
