// Browser-side decryption for the encrypted image archive, using only WebCrypto.
//
// Mirrors src/image_crawler/crypto.py and src/image_crawler/layout.py; keep them in sync.
//   blob  = MAGIC(4) | nonce(12) | AES-256-GCM(header | metadata | content | padding)
//   header = metadata length (u32 BE) | content length (u64 BE)
//   AAD (v2) = "ICV2|<kind>|<name>",  AAD (v1, images only) = "ICV1"

const MAGIC_V1 = "ICV1";
const MAGIC = "ICV2";
const NONCE_SIZE = 12;
const HEADER_SIZE = 12;

export const KIND_IMAGE = "image";
export const KIND_THUMB = "thumb";
export const KIND_INDEX = "index";

const text = new TextEncoder();
const utf8 = new TextDecoder();

export class DecryptionError extends Error {}

/** The AES key the viewer needs, derived from the 32-byte master key (same HKDF as the crawler). */
export async function deriveEncryptionKey(masterKey) {
  const base = await crypto.subtle.importKey("raw", masterKey, "HKDF", false, ["deriveKey"]);
  return crypto.subtle.deriveKey(
    { name: "HKDF", hash: "SHA-256", salt: new Uint8Array(0), info: text.encode("image-crawler/v1/encrypt") },
    base,
    { name: "AES-GCM", length: 256 },
    false,
    ["decrypt"],
  );
}

/** Import an already-derived 32-byte encryption key (what the phone will be given). */
export function importEncryptionKey(rawKey) {
  return crypto.subtle.importKey("raw", rawKey, { name: "AES-GCM" }, false, ["decrypt"]);
}

/** Decrypt a blob stored in the slot (kind, name). Returns { metadata, content }. */
export async function openBlob(key, blob, { kind, name }) {
  const bytes = blob instanceof Uint8Array ? blob : new Uint8Array(blob);
  const magic = utf8.decode(bytes.subarray(0, 4));
  let aad;
  if (magic === MAGIC) aad = text.encode(`${MAGIC}|${kind}|${name}`);
  else if (magic === MAGIC_V1 && kind === KIND_IMAGE) aad = text.encode(MAGIC_V1);
  else throw new DecryptionError("not an image-crawler blob");
  if (bytes.length < 4 + NONCE_SIZE) throw new DecryptionError("truncated blob");

  let body;
  try {
    body = new Uint8Array(
      await crypto.subtle.decrypt(
        { name: "AES-GCM", iv: bytes.subarray(4, 4 + NONCE_SIZE), additionalData: aad },
        key,
        bytes.subarray(4 + NONCE_SIZE),
      ),
    );
  } catch {
    throw new DecryptionError("wrong key, wrong slot, or corrupted blob");
  }
  const view = new DataView(body.buffer, body.byteOffset, body.byteLength);
  const metaLen = view.getUint32(0);
  const contentLen = Number(view.getBigUint64(4));
  const metaEnd = HEADER_SIZE + metaLen;
  return {
    metadata: JSON.parse(utf8.decode(body.subarray(HEADER_SIZE, metaEnd))),
    content: body.subarray(metaEnd, metaEnd + contentLen),
  };
}

// --- layout -------------------------------------------------------------

export const INDEX_PREFIX = "index/";
export const imageKey = (name) => `${name.slice(0, 2)}/${name}.bin`;
export const thumbKey = (name) => `thumbs/${name.slice(0, 2)}/${name}.bin`;
export const indexKey = (segment) => `${INDEX_PREFIX}${segment}.bin`;

/** Segment id from an index key, or null if the key isn't an index segment. */
export function indexSegmentId(key) {
  const m = /^index\/([0-9A-Za-z_-]+)\.bin$/.exec(key);
  return m ? m[1] : null;
}

/** Decrypt index segments ({ key, blob } pairs) into entries, oldest segment first, deduped. */
export async function readIndex(key, segments) {
  const sorted = [...segments].sort((a, b) => (a.key < b.key ? -1 : a.key > b.key ? 1 : 0));
  const seen = new Set();
  const entries = [];
  for (const { key: objectKey, blob } of sorted) {
    const segment = indexSegmentId(objectKey);
    if (segment === null) continue;
    const { content } = await openBlob(key, blob, { kind: KIND_INDEX, name: segment });
    for (const entry of JSON.parse(utf8.decode(content)).entries) {
      if (!seen.has(entry.object)) {
        seen.add(entry.object);
        entries.push(entry);
      }
    }
  }
  return entries;
}
