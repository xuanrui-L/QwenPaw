import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import "../../../../../player/ivb/static/authored-player.js";

const runtime = (
  globalThis as typeof globalThis & {
    IVBAuthoredPlayer: {
      mount(
        container: HTMLElement,
        bundle: object,
        adapter: object,
      ): {
        dispose(): void;
        show(screen: string): void;
      };
    };
  }
).IVBAuthoredPlayer;
let dispose: (() => void) | undefined;
afterEach(() => {
  dispose?.();
  document.body.replaceChildren();
  vi.restoreAllMocks();
});

it("keeps an unexplored map visible, gates jumps and reveals resume after starting", async () => {
  const html = readFileSync(
    resolve(
      process.cwd(),
      "../player/tests/fixtures/authored-presentation.html",
    ),
    "utf8",
  ).replace(
    "__NODES__",
    '<button data-action="jump" data-node-ref="entry">Entry</button><button data-action="jump" data-node-ref="ending">Ending</button>',
  );
  const container = document.createElement("div");
  document.body.append(container);
  const visit = vi.fn();
  const view = runtime.mount(
    container,
    {
      authored_html: html,
      meta: { title: "Story", synopsis: "Two routes" },
      entry_timeline_id: "entry",
      nodes: {
        entry: { title: "Entry", children: ["ending"] },
        ending: { title: "Ending", children: [] },
      },
      segments: { entry: "/entry.mp4", ending: "/ending.mp4" },
    },
    { visit },
  );
  dispose = () => view.dispose();
  const frame = container.querySelector("iframe")!;
  const doc = frame.contentDocument!;
  doc.open();
  doc.write(html);
  doc.close();
  const video = doc.querySelector("video")!;
  vi.spyOn(video, "pause").mockImplementation(() => {});
  vi.spyOn(video, "load").mockImplementation(() => {});
  vi.spyOn(video, "play").mockResolvedValue();
  frame.dispatchEvent(new Event("load"));
  const resume = doc.querySelector<HTMLButtonElement>(
    '[data-action="resume"]',
  )!;
  const entry = doc.querySelector<HTMLButtonElement>(
    '[data-node-ref="entry"]',
  )!;
  const ending = doc.querySelector<HTMLButtonElement>(
    '[data-node-ref="ending"]',
  )!;
  expect(resume).toHaveAttribute("data-host-hidden");
  view.show("map");
  for (const node of [entry, ending]) {
    expect(node).not.toHaveAttribute("data-host-hidden");
    expect(node).toBeDisabled();
  }
  ending.click();
  expect(visit).not.toHaveBeenCalled();
  view.show("title");
  doc.querySelector<HTMLButtonElement>('[data-action="start"]')!.click();
  await waitFor(() => expect(visit).toHaveBeenCalledWith("entry", undefined));
  await waitFor(() => expect(resume).not.toHaveAttribute("data-host-hidden"));
  view.show("map");
  expect(entry).toHaveAttribute("data-visited");
  expect(entry).toBeEnabled();
  expect(ending).not.toHaveAttribute("data-visited");
  expect(ending).toBeDisabled();
});

it.each(["", "/real-generated.mp4"])(
  "shows actual review media or an honest missing state (%s)",
  async (url) => {
    const html = readFileSync(
      resolve(
        process.cwd(),
        "../player/tests/fixtures/authored-presentation.html",
      ),
      "utf8",
    ).replace("__NODES__", "");
    const container = document.createElement("div");
    document.body.append(container);
    const inspect = vi.fn(),
      ending = vi.fn(),
      visit = vi.fn();
    const view = runtime.mount(
      container,
      {
        authored_html: html,
        meta: { title: "Story" },
        entry_timeline_id: "entry",
        nodes: { entry: { title: "Entry", children: [] } },
      },
      {
        review: true,
        segmentUrl: () => url,
        onInspect: inspect,
        ending,
        visit,
      },
    );
    dispose = () => view.dispose();
    const frame = container.querySelector("iframe")!;
    const doc = frame.contentDocument!;
    doc.open();
    doc.write(html);
    doc.close();
    const video = doc.querySelector("video")!;
    vi.spyOn(video, "pause").mockImplementation(() => {});
    const load = vi.spyOn(video, "load").mockImplementation(() => {});
    frame.dispatchEvent(new Event("load"));
    view.show("play");
    expect(video.getAttribute("src") || "").toBe(url);
    expect(video.poster).toBe("");
    expect(video.controls).toBe(true);
    expect(
      doc.querySelector("[data-editor-missing-media]")?.textContent || "",
    ).toBe(url ? "" : "当前节点尚未合成视频");
    view.show("play");
    expect(load).toHaveBeenCalledTimes(url ? 1 : 0);
    video.dispatchEvent(new Event("ended"));
    doc.querySelector<HTMLButtonElement>('[data-action="start"]')!.click();
    expect(inspect).toHaveBeenCalledWith({ screen: "title", action: "start" });
    expect(visit).not.toHaveBeenCalled();
    expect(ending).not.toHaveBeenCalled();
  },
);

it.each([true, false])(
  "preserves the viewer's playback state across map navigation (paused=%s)",
  async (userPaused) => {
    const html = readFileSync(
      resolve(
        process.cwd(),
        "../player/tests/fixtures/authored-presentation.html",
      ),
      "utf8",
    ).replace("__NODES__", "");
    const container = document.createElement("div");
    document.body.append(container);
    const view = runtime.mount(
      container,
      {
        authored_html: html,
        meta: { title: "Story" },
        entry_timeline_id: "entry",
        nodes: { entry: { title: "Entry", children: [] } },
        segments: { entry: "/entry.mp4" },
      },
      {},
    );
    dispose = () => view.dispose();
    const frame = container.querySelector("iframe")!;
    const doc = frame.contentDocument!;
    doc.open();
    doc.write(html);
    doc.close();
    const video = doc.querySelector("video")!;
    let paused = true;
    vi.spyOn(video, "paused", "get").mockImplementation(() => paused);
    vi.spyOn(video, "pause").mockImplementation(() => {
      paused = true;
    });
    vi.spyOn(video, "load").mockImplementation(() => {});
    vi.spyOn(video, "play").mockImplementation(async () => {
      paused = false;
    });
    frame.dispatchEvent(new Event("load"));
    const click = async (action: string) => {
      doc
        .querySelector<HTMLButtonElement>(`button[data-action="${action}"]`)!
        .click();
      await new Promise((resolve) => setTimeout(resolve, 0));
    };
    await click("start");
    expect(paused).toBe(false);
    if (userPaused) await click("toggle_play");
    await click("map");
    expect(paused).toBe(true);
    expect(doc.documentElement.dataset.currentScreen).toBe("map");
    await click("map_back");
    expect(doc.documentElement.dataset.currentScreen).toBe("play");
    expect(paused).toBe(userPaused);
  },
);
