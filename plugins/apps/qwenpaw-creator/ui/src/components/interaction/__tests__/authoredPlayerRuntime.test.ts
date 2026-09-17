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
  vi.unstubAllGlobals();
});

it("reveals only visited nodes and their masked frontier, including connections", async () => {
  const html = readFileSync(
    resolve(
      process.cwd(),
      "../player/tests/fixtures/authored-presentation.html",
    ),
    "utf8",
  ).replace(
    "__NODES__",
    '<button data-action="jump" data-node-ref="entry"><span data-node-label>来信</span><p>不要泄漏这段剧情</p></button><button data-action="jump" data-node-ref="ending" title="隐藏真相">Ending</button><button data-action="jump" data-node-ref="future">Future</button><svg data-map-from="entry" data-map-to="ending"></svg><svg data-map-from="ending" data-map-to="future"></svg>',
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
        ending: { title: "Ending", children: ["future"] },
        future: { title: "Future", children: [] },
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
  expect(entry).not.toHaveAttribute("data-host-hidden");
  expect(entry.textContent).toBe("？");
  expect(entry).toBeDisabled();
  expect(ending).toHaveAttribute("data-host-hidden");
  expect(ending).toHaveStyle({ display: "none" });
  expect(ending).not.toHaveAttribute("title");
  const future = doc.querySelector('[data-node-ref="future"]')!;
  expect(future).toHaveAttribute("data-host-hidden");
  expect(doc.querySelector('[data-map-from="entry"]')).toHaveAttribute(
    "data-host-hidden",
  );
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
  expect(ending).not.toHaveAttribute("data-host-hidden");
  expect(ending.style.display).not.toBe("none");
  expect(ending.textContent).toBe("？");
  expect(entry.textContent).toBe("来信");
  expect(future).toHaveAttribute("data-host-hidden");
  expect(doc.querySelector('[data-map-from="entry"]')).not.toHaveAttribute(
    "data-host-hidden",
  );
  expect(doc.querySelector('[data-map-from="ending"]')).toHaveAttribute(
    "data-host-hidden",
  );
  expect(doc.querySelector('[data-screen="map"]')!.textContent).not.toContain(
    "不要泄漏",
  );
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

function setupPlayer(extra: object = {}, adapter: object = {}) {
  const html = readFileSync(
    resolve(
      process.cwd(),
      "../player/tests/fixtures/authored-presentation.html",
    ),
    "utf8",
  )
    .replace(
      "__NODES__",
      '<div data-node-ref="entry"><button data-action="jump" data-node-ref="entry"><span data-node-label>来信</span><p>剧情梗概</p></button></div>',
    )
    .replace(
      'data-bind="project.title"></',
      'data-bind="project.title">未发送</',
    )
    .replace(
      'data-bind="project.synopsis"></',
      'data-bind="project.synopsis">这一刻，你会按下发送吗？</',
    )
    .replace('data-action="resume"', 'data-action="reset"');
  const container = document.createElement("div");
  document.body.append(container);
  const view = runtime.mount(
    container,
    {
      authored_html: html,
      meta: {
        title: "制作一个关于消息的故事",
        title_source: "auto",
        synopsis: "全部剧透",
      },
      entry_timeline_id: "entry",
      nodes: { entry: { title: "结局3：剧透", children: [] } },
      segments: { entry: "/entry.mp4" },
      ...extra,
    },
    adapter,
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
  const click = async (action: string) => {
    doc.querySelector<HTMLButtonElement>(`[data-action="${action}"]`)!.click();
    await new Promise((resolve) => setTimeout(resolve, 0));
  };
  return { doc, video, view, click, container };
}

it.each(["user", "auto"])(
  "uses the %s title policy, authored teaser and generic ending heading",
  async (source) => {
    const { doc, video, click } = setupPlayer({
      meta: { title: "用户项目名", title_source: source, synopsis: "全部剧透" },
    });
    expect(doc.querySelector('[data-bind="project.title"]')!.textContent).toBe(
      source === "user" ? "用户项目名" : "未发送",
    );
    expect(
      doc.querySelector('[data-bind="project.synopsis"]')!.textContent,
    ).toBe("这一刻，你会按下发送吗？");
    await click("start");
    video.dispatchEvent(new Event("ended"));
    await waitFor(() =>
      expect(doc.documentElement.dataset.currentScreen).toBe("ending"),
    );
    expect(
      doc.querySelector('[data-screen="ending"] [data-bind="node.title"]')!
        .textContent,
    ).toBe("结局");
    await click("reset");
    expect(doc.querySelector('[data-node-ref="entry"]')!.textContent).toBe(
      "？",
    );
  },
);

it("restores a short nested map label after visiting and supports delegated jump buttons", async () => {
  const visit = vi.fn();
  const { doc, click, view } = setupPlayer({}, { visit });
  await click("start");
  view.show("map");
  expect(doc.querySelector('[data-node-ref="entry"]')!.textContent).toBe(
    "来信回看",
  );
  expect(doc.querySelector('[data-action="jump"]')).toBeEnabled();
  await click("jump");
  expect(visit).toHaveBeenCalledTimes(2);
});

it.each(["standard", "webkit"])(
  "waits for %s fullscreen to close before mounting choices and ignores duplicate events",
  async (mode) => {
    const mount = vi.fn(() => ({ pause: vi.fn(), dispose: vi.fn() }));
    vi.stubGlobal("IVBInteraction", { mount });
    const { doc, video, click } = setupPlayer({
      interactions: [
        { source_timeline_id: "entry", at_seconds: 1, options: [] },
      ],
    });
    await click("start");
    let finish!: () => void;
    const exit = vi.fn();
    if (mode === "standard") {
      Object.defineProperty(doc, "fullscreenElement", {
        configurable: true,
        get: () => video,
      });
      Object.defineProperty(doc, "exitFullscreen", {
        configurable: true,
        value: exit.mockImplementation(
          () =>
            new Promise<void>((resolve) => {
              finish = resolve;
            }),
        ),
      });
    } else {
      Object.defineProperty(video, "webkitDisplayingFullscreen", {
        configurable: true,
        value: true,
      });
      Object.defineProperty(video, "webkitExitFullscreen", { value: exit });
      finish = () => {
        Object.defineProperty(video, "webkitDisplayingFullscreen", {
          value: false,
        });
        video.dispatchEvent(new Event("webkitendfullscreen"));
      };
    }
    video.currentTime = 2;
    video.dispatchEvent(new Event("timeupdate"));
    video.dispatchEvent(new Event("ended"));
    expect(exit).toHaveBeenCalledTimes(1);
    expect(mount).not.toHaveBeenCalled();
    finish();
    await waitFor(() => expect(mount).toHaveBeenCalledTimes(1));
    expect(doc.querySelector('[data-slot="interaction"]')).not.toHaveAttribute(
      "data-host-hidden",
    );
  },
);

it("reports fullscreen failure without starting an invisible countdown", async () => {
  const mount = vi.fn();
  vi.stubGlobal("IVBInteraction", { mount });
  const onError = vi.fn();
  const { doc, video, click } = setupPlayer(
    {
      interactions: [
        { source_timeline_id: "entry", at_seconds: 1, options: [] },
      ],
    },
    { onError },
  );
  await click("start");
  Object.defineProperty(doc, "fullscreenElement", { value: video });
  Object.defineProperty(doc, "exitFullscreen", {
    value: vi.fn().mockRejectedValue(new Error("fullscreen failed")),
  });
  video.dispatchEvent(new Event("ended"));
  await waitFor(() => expect(onError).toHaveBeenCalled());
  expect(mount).not.toHaveBeenCalled();
  expect(document.querySelector('[role="alert"]')).toHaveTextContent(
    "fullscreen failed",
  );
});

it("keeps unknown placeholders visible when authored CSS hides labels and disabled controls", async () => {
  const { doc, view } = setupPlayer();
  const style = doc.createElement("style");
  style.textContent =
    "[data-unknown] [data-node-label]{display:none!important;opacity:0;visibility:hidden}[data-unknown] button{display:none}";
  doc.head.append(style);
  view.show("map");
  const node = doc.querySelector(
    '[data-screen="map"] [data-node-ref="entry"]',
  )!;
  expect(node.textContent).toBe("？");
  const label = node.querySelector<HTMLElement>("[data-node-label]")!;
  expect(doc.defaultView!.getComputedStyle(label).display).not.toBe("none");
  expect(label).toHaveStyle({ visibility: "visible", opacity: "1" });
});
