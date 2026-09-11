import { afterEach, expect, it, vi } from "vitest";
import "../../../../../player/ivb/static/interaction-runtime.js";

type Runtime = {
  mount(
    container: HTMLElement,
    point: object,
    edges: object,
    select: (ref: string) => void,
  ): { dispose(): void; pause(value: boolean): void };
};
const runtime = (globalThis as typeof globalThis & { IVBInteraction: Runtime })
  .IVBInteraction;
let dispose: (() => void) | undefined;
afterEach(() => {
  dispose?.();
  document.body.replaceChildren();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

it.each([false, true])(
  "preserves authored buttons and distinguishes playback/review selection (%s)",
  (review) => {
    const container = document.createElement("div");
    document.body.append(container);
    const html =
      '<div data-question="old"><p class="question">old question</p><button data-edge-ref="edge:a"><span data-option-label>old label</span></button></div>';
    const selected: string[] = [];
    const view = runtime.mount(
      container,
      {
        question: "Authoritative question",
        options: [{ edge_ref: "edge:a" }],
        motion_html: html,
        review,
      },
      { "edge:a": { label: "Authoritative label" } },
      (ref) => selected.push(ref),
    );
    dispose = () => view.dispose();
    const frame = container.querySelector("iframe")!;
    // jsdom does not load srcdoc. Populate the real iframe document, then run
    // the same onload binding that Chromium executes after loading srcdoc.
    frame.contentDocument!.open();
    frame.contentDocument!.write(html);
    frame.contentDocument!.close();
    frame.dispatchEvent(new Event("load"));
    expect(container.querySelector("iframe")).toBe(frame);
    expect(frame.contentDocument!.querySelector(".question")?.textContent).toBe(
      "Authoritative question",
    );
    const button = frame.contentDocument!.querySelector("button")!;
    expect(button.textContent).toBe("Authoritative label");
    button.click();
    button.click();
    expect(selected).toEqual(review ? ["edge:a", "edge:a"] : ["edge:a"]);
  },
);

it.each([false, true])(
  "keeps authored countdown visuals and branch timing synchronized (%s review)",
  (review) => {
    vi.useFakeTimers({
      toFake: ["setInterval", "clearInterval", "performance"],
    });
    let hidden = false;
    vi.spyOn(document, "hidden", "get").mockImplementation(() => hidden);
    const container = document.createElement("div");
    document.body.append(container);
    const html =
      '<span data-interaction-countdown></span><button data-edge-ref="edge:a">A</button>';
    const selected = vi.fn();
    const view = runtime.mount(
      container,
      {
        question: "Choose",
        options: [{ edge_ref: "edge:a" }],
        motion_html: html,
        countdown_seconds: 6,
        default_edge_ref: "edge:a",
        review,
      },
      { "edge:a": { label: "A" } },
      selected,
    );
    dispose = () => view.dispose();
    vi.advanceTimersByTime(900); // Loading the iframe must not spend choice time.
    const frame = container.querySelector("iframe")!;
    const doc = frame.contentDocument!;
    doc.open();
    doc.write(html);
    doc.close();
    frame.dispatchEvent(new Event("load"));
    const ratio = () =>
      Number(
        doc.documentElement.style.getPropertyValue(
          "--interaction-countdown-ratio",
        ),
      );
    const seconds = () =>
      doc.querySelector("[data-interaction-countdown]")!.textContent;
    expect(ratio()).toBe(1);
    expect(seconds()).toBe("6s");
    vi.advanceTimersByTime(2000);
    expect(ratio()).toBeCloseTo(review ? 1 : 2 / 3);
    const held = ratio();
    view.pause(true); // Opening the story map pauses both outputs.
    vi.advanceTimersByTime(8000);
    expect(ratio()).toBe(held);
    expect(selected).not.toHaveBeenCalled();
    view.pause(false);
    hidden = true;
    document.dispatchEvent(new Event("visibilitychange"));
    vi.advanceTimersByTime(8000);
    expect(ratio()).toBe(held);
    hidden = false;
    document.dispatchEvent(new Event("visibilitychange"));
    vi.advanceTimersByTime(4100);
    expect(ratio()).toBe(review ? 1 : 0);
    expect(seconds()).toBe(review ? "6s" : "0s");
    expect(selected).toHaveBeenCalledTimes(review ? 0 : 1);
    vi.advanceTimersByTime(8000);
    expect(selected).toHaveBeenCalledTimes(review ? 0 : 1);
  },
);
