import { makeFeedbackScreenshot } from "@/api/creator/feedback";

async function renderViewport(signal: AbortSignal) {
  const { default: render } = await import("html2canvas-pro");
  const { innerWidth: width, innerHeight: height } = window;
  const canvas = await render(document.documentElement, {
    x: window.scrollX,
    y: window.scrollY,
    width,
    height,
    scale: Math.min(
      window.devicePixelRatio || 1,
      1920 / Math.max(width, height),
    ),
    useCORS: true,
    logging: false,
    imageTimeout: 1500,
    signal,
  });
  const blob = await new Promise<Blob>((resolve, reject) =>
    canvas.toBlob(
      (image) =>
        image ? resolve(image) : reject(new Error("Empty screenshot")),
      "image/jpeg",
      0.9,
    ),
  );
  return makeFeedbackScreenshot(
    new File([blob], "creator-workspace.jpg", { type: blob.type }),
  );
}

/** Capture just this Creator frame, before mounting the feedback dialog. */
export async function captureFeedbackScreenshot() {
  const controller = new AbortController();
  let timer: number | undefined;
  try {
    return await Promise.race([
      renderViewport(controller.signal),
      new Promise<never>((_, reject) => {
        timer = window.setTimeout(() => {
          controller.abort();
          reject(new Error("Screenshot timed out"));
        }, 5000);
      }),
    ]);
  } finally {
    window.clearTimeout(timer);
  }
}
