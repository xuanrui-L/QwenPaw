import {
  makeFeedbackScreenshot,
  type FeedbackScreenshot,
} from "@/api/creator/feedback";

async function renderViewport(signal: AbortSignal) {
  const { default: render } = await import("html2canvas-pro");
  const { innerWidth: width, innerHeight: height } = window;
  // DOM capture cannot read the compositor's current frame from an isolated player.
  const partialPreview = Array.from(
    document.querySelectorAll<HTMLIFrameElement>(
      "iframe[data-live-motion-player], iframe[data-live-motion-overlay]",
    ),
  ).some((frame) => {
    const rect = frame.getBoundingClientRect();
    return (
      rect.width > 0 &&
      rect.height > 0 &&
      rect.right > 0 &&
      rect.bottom > 0 &&
      rect.left < width &&
      rect.top < height &&
      getComputedStyle(frame).visibility === "visible"
    );
  });
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
    ignoreElements: (node) => node.matches("iframe[data-live-motion-player]"),
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
  return {
    ...makeFeedbackScreenshot(
      new File([blob], "creator-workspace.jpg", { type: blob.type }),
    ),
    partialPreview,
  };
}

/** Capture just this Creator frame, before mounting the feedback dialog. */
export async function captureFeedbackScreenshot(): Promise<FeedbackScreenshot> {
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
