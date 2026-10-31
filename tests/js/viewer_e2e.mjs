// Run by test_viewer.py.
//   node viewer_e2e.mjs sigv4                       -> signatures for AWS's published SigV4 examples
//   node viewer_e2e.mjs open <bundle> <passphrase>  -> full viewer path: unlock bundle, list + fetch over
//                                                      HTTP with SigV4, decrypt index/thumbnails/originals
import { createHash } from "node:crypto";
import { openBundle } from "../../pwa/js/keystore.js";
import { BudgetExceeded, RequestBudget, S3Client, signRequest } from "../../pwa/js/s3.js";
import { INDEX_PREFIX, KIND_IMAGE, KIND_THUMB, imageKey, openBlob, readIndex, thumbKey } from "../../pwa/js/vault.js";

const [mode, ...args] = process.argv.slice(2);

if (mode === "sigv4") {
  const creds = { accessKeyId: "AKIAIOSFODNN7EXAMPLE", secretAccessKey: "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY" };
  const date = new Date("2013-05-24T00:00:00Z");
  const sig = async (url, headers) =>
    (await signRequest({ method: "GET", url, headers, region: "us-east-1", date, ...creds }))
      .Authorization.split("Signature=")[1];
  console.log(JSON.stringify({
    getObject: await sig("https://examplebucket.s3.amazonaws.com/test.txt", { Range: "bytes=0-9" }),
    listObjects: await sig("https://examplebucket.s3.amazonaws.com/?max-keys=2&prefix=J", {}),
  }));
} else if (mode === "budget") {
  let now = 0;
  const budget = new RequestBudget({ max: 3, windowMs: 1000, now: () => now });
  const log = [];
  for (const t of [0, 100, 200, 300, 999, 1000, 1100, 1150]) {
    now = t;
    try { budget.take(); log.push(`${t}:ok`); } catch (e) { log.push(`${t}:${e instanceof BudgetExceeded ? "blocked " + e.retryInMs : e}`); }
  }
  // A client with an exhausted budget refuses before touching the network.
  let fetched = 0;
  const s3 = new S3Client({ endpoint: "http://x", region: "auto", bucket: "b", prefix: "", accessKeyId: "a", secretAccessKey: "s" },
    { fetch: async () => { fetched++; throw new Error("unreachable"); }, budget: new RequestBudget({ max: 0 }) });
  const refused = await s3.get("k").then(() => false, (e) => e instanceof BudgetExceeded);
  console.log(JSON.stringify({ log, refused, fetched }));
} else if (mode === "open") {
  const [bundle, passphrase] = args;
  const { config, encryptionKey } = await openBundle(bundle, passphrase);
  const s3 = new S3Client(config);
  const segmentKeys = await s3.list(INDEX_PREFIX);
  const segments = await Promise.all(segmentKeys.map(async (key) => ({ key, blob: await s3.get(key) })));
  const entries = await readIndex(encryptionKey, segments);
  const images = [];
  for (const e of entries) {
    const image = await openBlob(encryptionKey, await s3.get(imageKey(e.object)), { kind: KIND_IMAGE, name: e.object });
    const thumb = e.thumb
      ? (await openBlob(encryptionKey, await s3.get(thumbKey(e.thumb)), { kind: KIND_THUMB, name: e.thumb })).content.length
      : null;
    images.push({ sha256: createHash("sha256").update(image.content).digest("hex"), thumb });
  }
  let missing = null;
  try { await s3.get("ab/does-not-exist.bin"); } catch (err) { missing = { status: err.status, code: err.code }; }
  const { accessKeyId, secretAccessKey, ...publicConfig } = config;
  console.log(JSON.stringify({ config: publicConfig, keyCount: (await s3.list()).length, entries, images, missing }));
}
