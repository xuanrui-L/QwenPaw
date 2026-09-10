import { afterEach, expect, it } from "vitest";
import "../../../../../player/ivb/static/interaction-runtime.js";

type Runtime = {
  mount(
    container: HTMLElement,
    point: object,
    edges: object,
    select: (ref: string) => void,
  ): { dispose(): void };
};
const runtime = (globalThis as typeof globalThis & { IVBInteraction: Runtime })
  .IVBInteraction;
let dispose: (() => void) | undefined;
afterEach(() => {
  dispose?.();
  document.body.replaceChildren();
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
