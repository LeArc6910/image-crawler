// Minimal S3 client for the viewer: AWS Signature V4 with WebCrypto, path-style URLs,
// ListObjectsV2 and GetObject only. Works with Cloudflare R2 (region "auto").

const text = new TextEncoder();
const EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";

const hex = (buf) => [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
const sha256 = async (data) => hex(await crypto.subtle.digest("SHA-256", typeof data === "string" ? text.encode(data) : data));

async function hmac(key, data) {
  const k = await crypto.subtle.importKey("raw", typeof key === "string" ? text.encode(key) : key,
    { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  return crypto.subtle.sign("HMAC", k, text.encode(data));
}

/** RFC 3986 encoding as S3 expects it (encodeURIComponent leaves !'()* alone). */
export const uriEncode = (s) =>
  encodeURIComponent(s).replace(/[!'()*]/g, (c) => "%" + c.charCodeAt(0).toString(16).toUpperCase());

const amzDate = (date) => date.toISOString().replace(/[-:]/g, "").replace(/\.\d{3}/, "");

/**
 * Sign a request (headers only, empty body). Returns the headers to send, including Authorization.
 * `date` is injectable for tests.
 */
export async function signRequest({ method, url, headers = {}, region, service = "s3",
  accessKeyId, secretAccessKey, date = new Date() }) {
  const u = new URL(url);
  const stamp = amzDate(date);
  const day = stamp.slice(0, 8);
  const all = { ...headers, host: u.host, "x-amz-content-sha256": EMPTY_SHA256, "x-amz-date": stamp };
  const names = Object.keys(all).map((h) => h.toLowerCase()).sort();
  const lower = Object.fromEntries(Object.entries(all).map(([k, v]) => [k.toLowerCase(), String(v).trim()]));

  const query = [...u.searchParams.entries()]
    .map(([k, v]) => [uriEncode(k), uriEncode(v)])
    .sort(([a, x], [b, y]) => (a < b ? -1 : a > b ? 1 : x < y ? -1 : x > y ? 1 : 0))
    .map(([k, v]) => `${k}=${v}`)
    .join("&");
  const canonical = [
    method,
    u.pathname, // already encoded by objectUrl()
    query,
    names.map((n) => `${n}:${lower[n]}\n`).join(""),
    names.join(";"),
    EMPTY_SHA256,
  ].join("\n");

  const scope = `${day}/${region}/${service}/aws4_request`;
  const toSign = ["AWS4-HMAC-SHA256", stamp, scope, await sha256(canonical)].join("\n");
  let key = await hmac(`AWS4${secretAccessKey}`, day);
  for (const part of [region, service, "aws4_request"]) key = await hmac(key, part);
  const signature = hex(await hmac(key, toSign));

  const out = { ...headers, "x-amz-content-sha256": EMPTY_SHA256, "x-amz-date": stamp };
  out.Authorization = `AWS4-HMAC-SHA256 Credential=${accessKeyId}/${scope}, ` +
    `SignedHeaders=${names.join(";")}, Signature=${signature}`;
  return out;
}

/**
 * Stops runaway request loops (a bug, a stuck retry) before they cost money: at most `max`
 * requests per rolling `windowMs`. Normal browsing stays far below the default.
 */
export class RequestBudget {
  constructor({ max = 3000, windowMs = 10 * 60 * 1000, now = () => Date.now() } = {}) {
    Object.assign(this, { max, windowMs, now });
    this.times = [];
  }

  take() {
    const t = this.now();
    while (this.times.length && this.times[0] <= t - this.windowMs) this.times.shift();
    if (this.times.length >= this.max) throw new BudgetExceeded(this.times[0] + this.windowMs - t);
    this.times.push(t);
  }
}

export class BudgetExceeded extends Error {
  constructor(retryInMs) {
    super("request budget exceeded");
    this.retryInMs = retryInMs;
  }
}

export class S3Error extends Error {
  constructor(status, code, message) {
    super(`${status} ${code || ""} ${message || ""}`.trim());
    this.status = status;
    this.code = code;
  }
}

const xmlText = (xml, tag) => {
  const m = new RegExp(`<${tag}>([\\s\\S]*?)</${tag}>`).exec(xml);
  return m ? decodeXml(m[1]) : null;
};
const decodeXml = (s) => s.replace(/&(lt|gt|quot|apos|amp|#(\d+)|#x([0-9a-f]+));/gi, (_, name, dec, hx) =>
  dec ? String.fromCodePoint(+dec) : hx ? String.fromCodePoint(parseInt(hx, 16))
    : { lt: "<", gt: ">", quot: '"', apos: "'", amp: "&" }[name]);

/**
 * config: { endpoint, region, bucket, prefix, accessKeyId, secretAccessKey }
 * Keys passed to get()/list() are relative to `prefix`, like the crawler's storage keys.
 */
export class S3Client {
  constructor(config, { fetch: fetchImpl = globalThis.fetch.bind(globalThis), budget = new RequestBudget() } = {}) {
    this.c = config;
    this.fetch = fetchImpl;
    this.budget = budget;
    this.requests = 0;
  }

  objectUrl(key = "") {
    const path = key.split("/").map(uriEncode).join("/");
    return `${this.c.endpoint}/${uriEncode(this.c.bucket)}/${path}`;
  }

  async request(url, init = {}) {
    this.budget.take();
    this.requests++;
    const headers = await signRequest({
      method: "GET", url, headers: init.headers || {}, region: this.c.region,
      accessKeyId: this.c.accessKeyId, secretAccessKey: this.c.secretAccessKey,
    });
    const resp = await this.fetch(url, { method: "GET", headers, signal: init.signal });
    if (!resp.ok) {
      const body = await resp.text().catch(() => "");
      throw new S3Error(resp.status, xmlText(body, "Code"), xmlText(body, "Message"));
    }
    return resp;
  }

  /** One ListObjectsV2 page: { keys, next } (next is null on the last page). */
  async listPage(prefix = "", continuationToken = null) {
    const url = new URL(`${this.c.endpoint}/${uriEncode(this.c.bucket)}`);
    url.searchParams.set("list-type", "2");
    url.searchParams.set("prefix", this.c.prefix + prefix);
    if (continuationToken) url.searchParams.set("continuation-token", continuationToken);
    const xml = await (await this.request(url.toString())).text();
    const keys = [...xml.matchAll(/<Contents>[\s\S]*?<Key>([\s\S]*?)<\/Key>/g)]
      .map((m) => decodeXml(m[1]).slice(this.c.prefix.length));
    const truncated = xmlText(xml, "IsTruncated") === "true";
    return { keys, next: truncated ? xmlText(xml, "NextContinuationToken") : null };
  }

  async list(prefix = "") {
    const keys = [];
    let token = null;
    do {
      const page = await this.listPage(prefix, token);
      keys.push(...page.keys);
      token = page.next;
    } while (token);
    return keys;
  }

  async get(key, { signal } = {}) {
    const resp = await this.request(this.objectUrl(this.c.prefix + key), { signal });
    return new Uint8Array(await resp.arrayBuffer());
  }
}
