// Opens the passphrase-protected viewer bundle made by `image-crawler viewer-export`.
// Mirrors src/image_crawler/viewer.py; keep them in sync.
//   "ICVIEW1." + base64url( salt(16) | iterations(u32 BE) | nonce(12) | AES-GCM(json) ),  AAD "ICVIEW1"
// Only the sealed text is ever persisted; the passphrase is needed on every unlock.

import { importEncryptionKey } from "./vault.js";

const PREFIX = "ICVIEW1.";
const AAD = new TextEncoder().encode("ICVIEW1");

export class BundleError extends Error {}

export const fromBase64 = (s) => Uint8Array.from(atob(s), (c) => c.charCodeAt(0));
const fromBase64Url = (s) => fromBase64(s.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - (s.length % 4)) % 4));

export function looksLikeBundle(text) {
  return typeof text === "string" && text.replace(/\s+/g, "").startsWith(PREFIX);
}

/** Decrypt the bundle. Returns { config, encryptionKey } — the key is a non-extractable CryptoKey. */
export async function openBundle(text, passphrase) {
  const compact = text.replace(/\s+/g, "");
  if (!compact.startsWith(PREFIX)) throw new BundleError("not a viewer bundle");
  const blob = fromBase64Url(compact.slice(PREFIX.length));
  const salt = blob.subarray(0, 16);
  const iterations = new DataView(blob.buffer, blob.byteOffset + 16, 4).getUint32(0);
  const nonce = blob.subarray(20, 32);

  const base = await crypto.subtle.importKey("raw", new TextEncoder().encode(passphrase), "PBKDF2", false, ["deriveKey"]);
  const key = await crypto.subtle.deriveKey(
    { name: "PBKDF2", hash: "SHA-256", salt, iterations },
    base,
    { name: "AES-GCM", length: 256 },
    false,
    ["decrypt"],
  );
  let plain;
  try {
    plain = await crypto.subtle.decrypt({ name: "AES-GCM", iv: nonce, additionalData: AAD }, key, blob.subarray(32));
  } catch {
    throw new BundleError("wrong passphrase or damaged bundle");
  }
  const b = JSON.parse(new TextDecoder().decode(plain));
  if (b.v !== 1) throw new BundleError(`unsupported bundle version ${b.v}`);
  return {
    config: {
      endpoint: b.e, region: b.r, bucket: b.b, prefix: b.p,
      accessKeyId: b.a, secretAccessKey: b.s,
    },
    encryptionKey: await importEncryptionKey(fromBase64(b.k)),
  };
}
