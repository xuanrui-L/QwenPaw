import { act, renderHook, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { createElement, type ReactNode } from "react";
import { message } from "antd";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useProjectLaunch, type AttachmentDraft } from "../useProjectLaunch";
import {
  countScriptContentMarkers,
  detectScriptUpload,
  looksLikeScriptName,
  scriptSuffixKind,
} from "@/lib/scriptUploadDetect";

// The launch orchestration is the unit under test, so the API client is
// mocked at the module boundary: assertions read the exact call arguments
// (script vs. material routing) instead of replaying fetch shapes.
const api = vi.hoisted(() => ({
  createProject: vi.fn(),
  createAssetImport: vi.fn(),
  getAssetImport: vi.fn(),
  getTask: vi.fn(),
  importTimelineScript: vi.fn(),
  ingestAssetFile: vi.fn(),
  ingestAssetValue: vi.fn(),
  sendCreatorMessage: vi.fn(),
  newClientId: vi.fn(),
}));

vi.mock("@/api/creator", async (importOriginal) => {
  const actual = await importOriginal<Record<string, unknown>>();
  return { ...actual, ...api };
});

// config=null keeps missingRequiredModels null, so canLaunch depends only on
// the description — the model gate is covered by ProjectComposer.test.tsx.
vi.mock("@/store/modelConfigStore", () => {
  const state = { config: null, refresh: () => {} };
  return {
    useModelConfigStore: (selector: (value: typeof state) => unknown) =>
      selector(state),
  };
});

vi.mock("@/store/launchUploadStore", () => {
  const state = {
    begin: vi.fn(),
    fileStarted: vi.fn(),
    fileFinished: vi.fn(),
    messaging: vi.fn(),
    finish: vi.fn(),
  };
  return { useLaunchUploadStore: { getState: () => state } };
});

const wrapper = ({ children }: { children: ReactNode }) =>
  createElement(MemoryRouter, null, children);

// Real detectScriptUpload: a name containing 剧本 short-circuits to "text"
// without reading bytes, and a .mp4 short-circuits to "none".
const scriptFile = () =>
  new File(["## 场 1\n\n小明：你好"], "我的剧本.md", { type: "text/markdown" });
const secondScriptFile = () =>
  new File(["## 场 2\n\n小红：再见"], "第二集剧本.md", {
    type: "text/markdown",
  });
const videoFile = () => new File(["bytes"], "cat.mp4", { type: "video/mp4" });

const fileAtts = (attachments: AttachmentDraft[]) =>
  attachments.filter(
    (att): att is Extract<AttachmentDraft, { kind: "file" }> =>
      att.kind === "file",
  );

const byName = (attachments: AttachmentDraft[], name: string) =>
  fileAtts(attachments).find((att) => att.file.name === name)!;

function setup() {
  return renderHook(() => useProjectLaunch(), { wrapper });
}

beforeEach(() => {
  vi.clearAllMocks();
  let seq = 0;
  api.newClientId.mockImplementation((prefix: string) => `${prefix}-${++seq}`);
  api.createProject.mockResolvedValue({
    projectId: "p1",
    creatorSessionId: "s1",
    conversationId: "c1",
  });
  api.importTimelineScript.mockResolvedValue({ artifactVersionId: "script-1" });
  api.ingestAssetFile.mockResolvedValue({
    assetVersionId: "av1",
    taskId: "t1",
  });
  api.sendCreatorMessage.mockResolvedValue({});
});

describe("useProjectLaunch script intake", () => {
  it("detects a script-like file but defaults every chip to material", async () => {
    const { result } = setup();
    act(() => {
      result.current.addFiles([scriptFile(), videoFile()]);
    });

    // Detection reveals the 剧本/素材 switch (scriptKind) but never opts in:
    // the default stays material until the user flips a chip.
    await waitFor(() =>
      expect(byName(result.current.attachments, "我的剧本.md").scriptKind).toBe(
        "text",
      ),
    );
    const script = byName(result.current.attachments, "我的剧本.md");
    const video = byName(result.current.attachments, "cat.mp4");
    expect(script.scriptKind).toBe("text");
    expect(script.asScript).toBe(false);
    expect(video.scriptKind).toBe("none");
    expect(video.asScript).toBe(false);
  });

  it("keeps a single script at creation: marking one clears the other", async () => {
    const { result } = setup();
    act(() => {
      result.current.addFiles([scriptFile(), secondScriptFile()]);
    });

    // Both are detected as script-like but neither is opted in by default.
    await waitFor(() =>
      expect(
        fileAtts(result.current.attachments).filter(
          (att) => att.scriptKind === "text",
        ),
      ).toHaveLength(2),
    );
    const first = byName(result.current.attachments, "我的剧本.md");
    const second = byName(result.current.attachments, "第二集剧本.md");
    expect(first.asScript).toBe(false);
    expect(second.asScript).toBe(false);

    // Opting the first in marks it as the script.
    act(() => {
      result.current.toggleAttachmentScript(first.id);
    });
    expect(
      fileAtts(result.current.attachments).find((a) => a.id === first.id)!
        .asScript,
    ).toBe(true);

    // Marking the second flips it on and the first off (mutually exclusive).
    act(() => {
      result.current.toggleAttachmentScript(second.id);
    });
    expect(
      fileAtts(result.current.attachments).find((a) => a.id === second.id)!
        .asScript,
    ).toBe(true);
    expect(
      fileAtts(result.current.attachments).find((a) => a.id === first.id)!
        .asScript,
    ).toBe(false);

    // Toggling the marked one again clears it entirely.
    act(() => {
      result.current.toggleAttachmentScript(second.id);
    });
    expect(
      fileAtts(result.current.attachments).find((a) => a.id === second.id)!
        .asScript,
    ).toBe(false);
  });

  it("lands the marked script on timeline:main and ingests other files as material", async () => {
    const { result } = setup();
    const script = scriptFile();
    const video = videoFile();
    act(() => {
      result.current.setProjectDescription("用剧本和素材做一支短片");
      result.current.addFiles([script, video]);
    });
    await waitFor(() =>
      expect(byName(result.current.attachments, "我的剧本.md").scriptKind).toBe(
        "text",
      ),
    );
    // Material is the default; opt the script in explicitly before launch.
    act(() => {
      result.current.toggleAttachmentScript(
        byName(result.current.attachments, "我的剧本.md").id,
      );
    });
    expect(byName(result.current.attachments, "我的剧本.md").asScript).toBe(
      true,
    );

    await act(async () => {
      await result.current.handleLaunch();
    });
    await waitFor(() =>
      expect(api.sendCreatorMessage).toHaveBeenCalledTimes(1),
    );

    // The script goes direct (no ProjectSource), everything else is material.
    expect(api.importTimelineScript).toHaveBeenCalledWith(
      "p1",
      "timeline:main",
      { file: script },
      expect.any(String),
    );
    expect(api.ingestAssetFile).toHaveBeenCalledTimes(1);
    expect(api.ingestAssetFile.mock.calls[0][1]).toBe(video);
    expect(api.ingestAssetFile.mock.calls[0][2]).toBe("NONE");

    // The first message references only material versions, never the script.
    const sent = api.sendCreatorMessage.mock.calls[0][1];
    expect(sent.assetVersionRefs).toEqual(["asset-version:av1"]);
  });

  it("falls back to material ingest and warns when the direct import fails", async () => {
    const warn = vi
      .spyOn(message, "warning")
      .mockImplementation(
        () => undefined as unknown as ReturnType<typeof message.warning>,
      );
    api.importTimelineScript.mockRejectedValue(new Error("boom"));
    api.ingestAssetFile.mockResolvedValue({
      assetVersionId: "av-fallback",
      taskId: "t9",
    });

    const { result } = setup();
    const script = scriptFile();
    act(() => {
      result.current.setProjectDescription("剧本直登失败要兜底");
      result.current.addFiles([script]);
    });
    await waitFor(() =>
      expect(byName(result.current.attachments, "我的剧本.md").scriptKind).toBe(
        "text",
      ),
    );
    // Material is the default; opt the script in explicitly before launch.
    act(() => {
      result.current.toggleAttachmentScript(
        byName(result.current.attachments, "我的剧本.md").id,
      );
    });
    expect(byName(result.current.attachments, "我的剧本.md").asScript).toBe(
      true,
    );

    await act(async () => {
      await result.current.handleLaunch();
    });
    await waitFor(() =>
      expect(api.sendCreatorMessage).toHaveBeenCalledTimes(1),
    );

    // The user's file is never lost: it degrades to a material ingest.
    expect(api.importTimelineScript).toHaveBeenCalledTimes(1);
    expect(api.ingestAssetFile).toHaveBeenCalledTimes(1);
    expect(api.ingestAssetFile.mock.calls[0][1]).toBe(script);
    expect(api.ingestAssetFile.mock.calls[0][2]).toBe("NONE");
    expect(warn).toHaveBeenCalled();
    const sent = api.sendCreatorMessage.mock.calls[0][1];
    expect(sent.assetVersionRefs).toEqual(["asset-version:av-fallback"]);
  });
});

// 一份典型剧本的首屏：标题、角色行、动作提示都齐了。
const SCRIPT_MARKDOWN = [
  "## 场 1 · 内景 · 旧宅大厅 · 夜",
  "**林晚**：我回来了。",
  "> 她看见母亲遗留的钥匙压在鞋下。",
  "林晚：钥匙还在。",
].join("\n");

// 普通笔记：没有任何剧本结构特征，绝不能因此打断用户。
const PROSE = [
  "今天开会讨论了下一季度的排期，大家同意先做导入链路。",
  "会后我整理了纪要，明天同步给制作组。",
].join("\n");

function textFile(name: string, content: string): File {
  return new File([content], name, { type: "text/plain" });
}

describe("scriptSuffixKind", () => {
  it("splits uploads into text, document and everything else", () => {
    const textNames = [
      "剧本.md",
      "a.markdown",
      "b.txt",
      "c.text",
      "d.rst",
      "e.srt",
      "f.vtt",
    ];
    for (const name of textNames) {
      expect(scriptSuffixKind(name), name).toBe("text");
    }
    for (const name of ["剧本.pdf", "剧本.doc", "剧本.docx"]) {
      expect(scriptSuffixKind(name), name).toBe("document");
    }
    for (const name of ["分镜.png", "片段.mp4", "设定.json", "无后缀", ""]) {
      expect(scriptSuffixKind(name), name).toBe("none");
    }
  });

  it("ignores the suffix casing", () => {
    expect(scriptSuffixKind("我的剧本.MD")).toBe("text");
    expect(scriptSuffixKind("我的剧本.Docx")).toBe("document");
  });
});

describe("looksLikeScriptName", () => {
  it("recognises script-ish file names", () => {
    for (const name of [
      "我的剧本.md",
      "拍摄脚本 v2.txt",
      "第三集 台词.docx",
      "第 12 场 修订.md",
      "screenplay_final.pdf",
      "EP07 分场.docx",
    ]) {
      expect(looksLikeScriptName(name), name).toBe(true);
    }
  });

  it("leaves ordinary uploads alone", () => {
    for (const name of [
      "会议记录.md",
      "notes.txt",
      "shot list.pdf",
      "角色设定表.docx",
    ]) {
      expect(looksLikeScriptName(name), name).toBe(false);
    }
  });
});

describe("countScriptContentMarkers", () => {
  it("counts the structural markers of a script", () => {
    expect(countScriptContentMarkers(SCRIPT_MARKDOWN)).toBe(4);
  });

  it("needs more than one marker before it looks like a script", () => {
    // 单条特征（一个场次标题）不足以打断用户。
    expect(countScriptContentMarkers("第一场 日 内 教室")).toBe(1);
    expect(countScriptContentMarkers(PROSE)).toBe(0);
    expect(countScriptContentMarkers("")).toBe(0);
  });
});

describe("detectScriptUpload", () => {
  it("never asks about files that cannot carry a script", async () => {
    await expect(
      detectScriptUpload(new File([SCRIPT_MARKDOWN], "分镜.png")),
    ).resolves.toBe("none");
  });

  it("asks for a text file whose name already says script", async () => {
    // 文件名就够了，正文是不是剧本都照问（默认项仍是素材理解）。
    const detected = await detectScriptUpload(textFile("我的剧本.md", PROSE));
    expect(detected).toBe("text");
  });

  it("asks for an unnamed text file whose first screen is a script", async () => {
    await expect(
      detectScriptUpload(textFile("未命名.txt", SCRIPT_MARKDOWN)),
    ).resolves.toBe("text");
  });

  it("stays silent for ordinary text uploads", async () => {
    const detected = await detectScriptUpload(textFile("会议记录.md", PROSE));
    expect(detected).toBe("none");
  });

  it("only reads the first screen of a text file", async () => {
    // 剧本特征出现在 64KB 之后：不该为了判定把整份文件读进内存。
    const late = `${"x".repeat(70 * 1024)}\n${SCRIPT_MARKDOWN}`;
    await expect(detectScriptUpload(textFile("notes.txt", late))).resolves.toBe(
      "none",
    );
  });

  it("judges Word/PDF by name alone", async () => {
    // 文档正文在浏览器里读不出来，只有文件名像剧本时才问。
    await expect(
      detectScriptUpload(new File(["PK\u0003\u0004"], "我的剧本.docx")),
    ).resolves.toBe("document");
    await expect(
      detectScriptUpload(new File(["%PDF-1.7"], "扫描件.pdf")),
    ).resolves.toBe("none");
  });
});
