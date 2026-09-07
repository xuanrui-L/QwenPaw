import { describe, expect, it } from "vitest";
import type { ProjectDocument } from "@/contracts/creator";
import {
  creatorReferenceLabel,
  creatorTargetLabel,
  creatorToolLabel,
  creatorWorkNodeLabel,
  getToolRunningLabel,
  getEstimatedDuration,
  humanizeCreatorRefs,
} from "@/lib/creatorPresentation";

const project = {
  timelines: {
    items: {
      t1: {
        title: "第一幕 · 清晨",
        name: "主时间轴",
        elements_by_id: { opening: { label: "推开窗户" } },
      },
      t2: {
        title: "第二幕 · 重逢",
        elements_by_id: { unnamed: { label: "unnamed" } },
      },
    },
  },
  visual: { entities: { items: { fox: { name: "小狐狸" } } } },
  assets: {
    source_versions_by_id: {
      v1: { name: "森林录音.wav", logical_asset_id: "audio" },
    },
    artifact_versions_by_id: {},
  },
  sources: { sources: { items: {} } },
} as unknown as ProjectDocument;

describe("Creator public reference labels", () => {
  it("resolves a real ungenerated visual variant to its owner instead of current project", () => {
    const withVariant = {
      ...project,
      visual: {
        entities: {
          items: {
            "char:woman": {
              name: "找钥匙的女子",
              variants: {
                order: ["variant:default"],
                items: {
                  "variant:default": {
                    requirements: "灰外套蓝包",
                    selected_artifact_version_id: null,
                  },
                },
              },
            },
          },
        },
      },
    } as unknown as ProjectDocument;
    const ref = "visual-variant:char:woman@variant:default";
    expect(creatorTargetLabel(ref, withVariant)).toBe("找钥匙的女子");
    expect(creatorReferenceLabel({ ref, name: ref }, withVariant)).toBe(
      "找钥匙的女子",
    );
    expect(
      humanizeCreatorRefs(`查看 [造型](${ref}) 与 \`${ref}\`。`, withVariant),
    ).toBe("查看 找钥匙的女子 与 找钥匙的女子。");
    expect(creatorTargetLabel(ref)).toBe("视觉设定");
  });
  it("resolves the exact timeline, element and source name", () => {
    expect(creatorTargetLabel("timeline:t2", project)).toBe("第二幕 · 重逢");
    expect(creatorTargetLabel("element:opening", project)).toBe("推开窗户");
    expect(creatorTargetLabel("asset:fox", project)).toBe("小狐狸");
    expect(creatorTargetLabel("asset:audio", project)).toBe("森林录音.wav");
    expect(
      creatorReferenceLabel(
        { ref: "element:opening", name: "旧镜头名称" },
        project,
      ),
    ).toBe("推开窗户");
  });

  it("does not fall back to machine ids or file paths in visible/accessibility labels", () => {
    for (const name of [
      "element:opening",
      "opening",
      "/tmp/private/project.json",
      "file:///tmp/private.json",
      '{"element_id":"opening"}',
    ]) {
      expect(
        creatorReferenceLabel({ ref: "element:opening", name }, project),
      ).toBe("推开窗户");
    }
    expect(creatorTargetLabel("element:unnamed", project)).toBe("时间线内容");
    expect(creatorTargetLabel("element:deleted", project)).toBe("时间线内容");
    expect(creatorTargetLabel("unknown:private", project)).toBe("当前项目");
    expect(
      creatorReferenceLabel(
        { ref: "asset-version:v1", name: "森林录音.wav" },
        project,
      ),
    ).toBe("森林录音.wav");
  });

  it("uses public names for plain and linked internal references while retaining external links", () => {
    expect(
      humanizeCreatorRefs("已调整 element:opening，使用 asset:fox。", project),
    ).toBe("已调整 推开窗户，使用 小狐狸。");
    expect(
      humanizeCreatorRefs(
        "查看 [element:opening](element:opening) 与 [文档](https://example.com/help)。",
        project,
      ),
    ).toBe("查看 推开窗户 与 [文档](https://example.com/help)。");
    expect(humanizeCreatorRefs("asset-version:gone", project)).toBe("素材版本");
  });
});

describe("Creator tool presentation facts", () => {
  it.each([
    ["view_skill", "查阅创作指南", "正在查阅创作指南…"],
    ["ground_image_objects", "识别画面对象", "正在识别画面对象…"],
    ["browser_use", "操作网页", "正在操作网页…"],
    ["computer_use", "操作桌面应用", "正在操作桌面应用…"],
  ])(
    "names the registered %s action and its running state",
    (tool, label, running) => {
      expect(creatorToolLabel(tool)).toBe(label);
      expect(getToolRunningLabel(tool)).toBe(running);
    },
  );

  it("names the actual guides read during production without exposing arbitrary skill names", () => {
    expect(
      creatorToolLabel("view_skill", { skill: "visual-asset-design" }),
    ).toBe("查阅视觉设计指南");
    expect(
      creatorToolLabel("view_skill", { skill: "professional-media-prompts" }),
    ).toBe("查阅提示词编写指南");
    expect(
      getToolRunningLabel("view_skill", { skill: "visual-asset-design" }),
    ).toBe("正在查阅视觉设计指南…");
    for (const skill of [
      "/private/internal-guide",
      "custom_skill_id",
      { name: "SECRET" },
      null,
    ]) {
      expect(creatorToolLabel("view_skill", { skill })).toBe("查阅创作指南");
      expect(getToolRunningLabel("view_skill", { skill })).toBe(
        "正在查阅创作指南…",
      );
    }
  });

  it("labels the patch_project tool observed in the real DashScope run", () => {
    expect(creatorToolLabel("patch_project")).toBe("更新视频方案");
    expect(getToolRunningLabel("patch_project")).toBe("正在修改项目…");
  });

  it("uses exact public object titles for work nodes and hides identifier fallbacks", () => {
    expect(
      creatorWorkNodeLabel(
        {
          id: "video:opening",
          kind: "video",
          label: "opening · 视频",
          locator: { elementId: "opening" },
        },
        project,
      ),
    ).toBe("推开窗户 · 视频生成");
    expect(
      creatorWorkNodeLabel(
        {
          id: "compose:t2",
          kind: "compose",
          label: "最终合成 (t2)",
          locator: {},
          timelineId: "t2",
        },
        project,
      ),
    ).toBe("第二幕 · 重逢 · 最终合成");
    expect(
      creatorWorkNodeLabel(
        {
          id: "lineup:cast-id",
          kind: "lineup",
          label: "cast-id 阵容图",
          locator: {},
        },
        project,
      ),
    ).toBe("阵容图");
    expect(
      creatorWorkNodeLabel(
        {
          id: "script:timeline:missing",
          kind: "script",
          label: "timeline:missing · 剧本",
          locator: { timelineId: "timeline:missing" },
        },
        project,
      ),
    ).toBe("剧本");
    expect(
      creatorWorkNodeLabel({
        id: "video:private-id",
        kind: "video",
        label: "开场镜头 · 视频",
        locator: { elementId: "private-id" },
      }),
    ).toBe("开场镜头 · 视频");
  });

  it("never invents a duration estimate from a tool name", () => {
    for (const tool of [
      "image_generation",
      "r2v_generation",
      "finalize_video",
      "unknown",
    ])
      expect(getEstimatedDuration(tool)).toBeNull();
  });

  it("labels actual source observation tools without exposing their internal identifiers", () => {
    for (const tool of [
      "read_source_video",
      "observe_source_clip",
      "check_observation_tasks",
      "review_scene",
    ])
      expect(creatorToolLabel(tool)).not.toMatch(/presentation\.|_/u);
  });
});
