// Camera QR scanning: the built-in BarcodeDetector where available (Android Chrome),
// otherwise the bundled jsQR decoder (iOS Safari), loaded only when needed.

let jsQRLoading = null;

function loadJsQR() {
  if (globalThis.jsQR) return Promise.resolve(globalThis.jsQR);
  jsQRLoading ??= new Promise((resolve, reject) => {
    const script = document.createElement("script");
    script.src = "vendor/jsqr/jsQR.js";
    script.onload = () => resolve(globalThis.jsQR);
    script.onerror = () => reject(new Error("QR 해독기를 불러오지 못했습니다"));
    document.head.append(script);
  });
  return jsQRLoading;
}

async function makeDetector() {
  if ("BarcodeDetector" in globalThis) {
    try {
      const formats = await globalThis.BarcodeDetector.getSupportedFormats();
      if (formats.includes("qr_code")) {
        const detector = new globalThis.BarcodeDetector({ formats: ["qr_code"] });
        return async (video) => (await detector.detect(video))[0]?.rawValue ?? null;
      }
    } catch { /* fall through to jsQR */ }
  }
  const jsQR = await loadJsQR();
  const canvas = document.createElement("canvas");
  const ctx = canvas.getContext("2d", { willReadFrequently: true });
  return async (video) => {
    canvas.width = video.videoWidth;
    canvas.height = video.videoHeight;
    if (!canvas.width) return null;
    ctx.drawImage(video, 0, 0);
    const { data, width, height } = ctx.getImageData(0, 0, canvas.width, canvas.height);
    return jsQR(data, width, height, { inversionAttempts: "attemptBoth" })?.data ?? null;
  };
}

/**
 * Scan until a QR code accepted by `accept(text)` is seen, or `signal` aborts.
 * Resolves with the text; the camera is always released.
 */
export async function scanQr(video, accept, signal) {
  const stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment" }, audio: false });
  try {
    video.srcObject = stream;
    await video.play();
    const detect = await makeDetector();
    while (!signal.aborted) {
      const text = await detect(video).catch(() => null);
      if (text && accept(text)) return text;
      await new Promise((r) => setTimeout(r, 200));
    }
    throw new DOMException("scan cancelled", "AbortError");
  } finally {
    stream.getTracks().forEach((t) => t.stop());
    video.srcObject = null;
  }
}
